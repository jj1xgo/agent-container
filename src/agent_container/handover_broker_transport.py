from dataclasses import replace
from datetime import datetime
from pathlib import Path
import struct
from typing import BinaryIO

from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import HandoverResponse
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_broker_protocol import decode_request_frame
from agent_container.handover_broker_protocol import encode_response_frame
from agent_container.handover_writer import create_atomic_handover
from agent_container.handover_writer import validate_handover_content


_AUTHORIZATION_BODY = """## 作業の目的
authorization
## 現在地
authorization
## 決定事項と理由
authorization
## 変更したファイル・commit・PR
authorization
## 検証結果
authorization
## 未解決事項とリスク
authorization
## 次の一手
authorization
"""


class _RequestFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_exact(connection: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = connection.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise _RequestFailure("schema") from None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(output):
            raise _RequestFailure("schema")
        output.extend(chunk)
    return bytes(output)


def _read_one_request(connection: BinaryIO) -> HandoverRequest:
    header = _read_exact(connection, 4)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise _RequestFailure("size")
    payload = _read_exact(connection, length)
    try:
        request, consumed = decode_request_frame(header + payload)
    except ValueError:
        raise _RequestFailure("schema") from None
    if consumed != len(header) + len(payload):
        raise _RequestFailure("schema")
    return request


def _write_response(
    connection: BinaryIO,
    response: HandoverResponse,
) -> bool:
    try:
        frame = encode_response_frame(response)
        offset = 0
        while offset < len(frame):
            written = connection.write(frame[offset:])
            if (
                isinstance(written, bool)
                or not isinstance(written, int)
                or written <= 0
                or written > len(frame) - offset
            ):
                raise ValueError("handover broker response write failed")
            offset += written
        connection.flush()
    except (OSError, TypeError, ValueError):
        return False
    return True


def _finish(
    session: HandoverBrokerSession,
    connection: BinaryIO,
    response: HandoverResponse,
    *,
    audit_status: str,
    audit_stage: str,
    audit_path: str = "",
    result: int,
) -> int:
    if not _write_response(connection, response):
        try:
            session.audit("error", stage="response")
        except (OSError, ValueError):
            pass
        return 1
    try:
        session.audit(audit_status, stage=audit_stage, path=audit_path)
    except (OSError, ValueError):
        return 1
    return result


def _failure_response(code: str) -> HandoverResponse:
    status = (
        "denied"
        if code in {"authentication", "schema", "size", "content-policy"}
        else "error"
    )
    return HandoverResponse(PROTOCOL_VERSION, status, "", code)


def handle_handover_connection(
    session: HandoverBrokerSession,
    connection: BinaryIO,
    peer_uid: int,
    now: datetime | None = None,
) -> int:
    try:
        try:
            request = _read_one_request(connection)
        except _RequestFailure as failure:
            response = _failure_response(failure.code)
            return _finish(
                session,
                connection,
                response,
                audit_status=response.status,
                audit_stage=failure.code,
                result=1,
            )

        authorization_request = replace(
            request,
            title="handover authorization",
            body=_AUTHORIZATION_BODY,
        )
        try:
            session.authorize(authorization_request, peer_uid)
        except ValueError:
            response = _failure_response("authentication")
            return _finish(
                session,
                connection,
                response,
                audit_status="denied",
                audit_stage="authentication",
                result=1,
            )

        try:
            title, body = validate_handover_content(request.title, request.body)
        except ValueError:
            response = _failure_response("content-policy")
            return _finish(
                session,
                connection,
                response,
                audit_status="denied",
                audit_stage="content-policy",
                result=1,
            )

        try:
            created = create_atomic_handover(
                session.project_dir,
                session.project_id,
                title,
                body,
                now=now,
            )
            if not isinstance(created, Path) or created.parent != session.project_dir:
                raise ValueError("handover writer returned an invalid path")
        except ValueError:
            response = _failure_response("filesystem-boundary")
            return _finish(
                session,
                connection,
                response,
                audit_status="error",
                audit_stage="filesystem-boundary",
                result=1,
            )
        except OSError:
            response = _failure_response("write")
            return _finish(
                session,
                connection,
                response,
                audit_status="error",
                audit_stage="write",
                result=1,
            )
        except Exception:
            response = _failure_response("unavailable")
            return _finish(
                session,
                connection,
                response,
                audit_status="error",
                audit_stage="unavailable",
                result=1,
            )

        container_path = f"/handovers/{session.project_id}/{created.name}"
        response = HandoverResponse(PROTOCOL_VERSION, "ok", container_path, "")
        return _finish(
            session,
            connection,
            response,
            audit_status="ok",
            audit_stage="write",
            audit_path=container_path,
            result=0,
        )
    finally:
        try:
            connection.close()
        except OSError:
            pass
