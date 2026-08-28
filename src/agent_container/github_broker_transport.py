from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from typing import BinaryIO, Iterable

from agent_container.git_remote_helper import MAX_STATELESS_REQUEST_BYTES
from agent_container.git_remote_helper import MAX_RECEIVE_PACK_REQUEST_BYTES
from agent_container.git_protocol import gate_receive_pack_commands
from agent_container.git_protocol import parse_receive_pack_advertisement
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import MAX_STREAM_CHUNK_BYTES
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import iter_chunk_stream
from agent_container.github_broker_protocol import read_request_frame
from agent_container.github_broker_protocol import read_response_frame
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_broker_policy import validate_issue_number
from agent_container.github_git_transport import MAX_DISCOVERY_BYTES
from agent_container.github_git_transport import MAX_UPLOAD_PACK_RESPONSE_BYTES
from agent_container.github_git_transport import GitHubUploadPackTransport
from agent_container.github_git_transport import GitHubReceivePackTransport
from agent_container.github_git_transport import MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES
from agent_container.github_git_transport import MAX_RECEIVE_PACK_RESPONSE_BYTES
from agent_container.github_issue import GitHubIssueTransport
from agent_container.github_issue import MAX_ISSUE_RESPONSE_BYTES
from agent_container.github_pr import GitHubPullRequestTransport
from agent_container.github_pr import MAX_PR_RESPONSE_BYTES


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PR_OPERATIONS = frozenset({"pr-create", "pr-view", "pr-checks"})
_ISSUE_OPERATIONS = frozenset({"issue-list", "issue-view"})


def _write_response(connection: BinaryIO, status: str) -> bool:
    try:
        connection.write(
            encode_response_frame(BrokerResponse(PROTOCOL_VERSION, status))
        )
        connection.flush()
    except (ValueError, OSError):
        return False
    return True


def _validate_exact_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("broker runtime path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("broker runtime path is invalid") from None
    if resolved != path:
        raise ValueError("broker runtime path must not contain symlinks")
    return resolved


def read_broker_capability(path: Path) -> str:
    _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_size > 45
    ):
        raise ValueError("broker capability file is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        body = os.read(descriptor, 46)
    finally:
        os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError("broker capability file is invalid") from None
    if _CAPABILITY.fullmatch(capability) is None or body != (capability + "\n").encode():
        raise ValueError("broker capability file is invalid")
    return capability


def validate_broker_socket(path: Path) -> Path:
    _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("broker socket is invalid")
    return path


@dataclass
class BrokerUploadPackClient:
    socket_path: Path
    capability_path: Path
    project_id: str
    repository: str
    socket_factory: object = socket.socket
    _socket: socket.socket | None = field(default=None, init=False, repr=False)
    _stream: BinaryIO | None = field(default=None, init=False, repr=False)
    _discovery: bytes | None = field(default=None, init=False, repr=False)

    def _connect(self) -> None:
        if self._stream is not None:
            return
        validate_broker_socket(self.socket_path)
        capability = read_broker_capability(self.capability_path)
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
        stream: BinaryIO | None = None
        try:
            client.settimeout(60)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            request = BrokerRequest(
                version=PROTOCOL_VERSION,
                capability=capability,
                project_id=self.project_id,
                sequence=secrets.randbelow((1 << 63) - 1) + 1,
                operation="git-upload-pack",
                payload={"repository": self.repository},
            )
            stream.write(encode_request_frame(request))
            stream.flush()
            response = read_response_frame(stream)
            if response.status != "ok":
                raise RuntimeError("Git broker request was denied")
            discovery = b"".join(
                iter_chunk_stream(stream, maximum_total=MAX_DISCOVERY_BYTES)
            )
            if not discovery:
                raise RuntimeError("Git broker discovery failed")
        except Exception:
            if stream is not None:
                stream.close()
            client.close()
            raise
        self._socket = client
        self._stream = stream
        self._discovery = discovery

    def discover(self) -> bytes:
        self._connect()
        assert self._discovery is not None
        return self._discovery

    def rpc(self, request: bytes) -> Iterable[bytes]:
        self._connect()
        if self._stream is None:
            raise RuntimeError("Git broker connection failed")
        if not isinstance(request, bytes) or not request:
            raise ValueError("Git broker request is invalid")
        chunks = (
            request[offset : offset + MAX_STREAM_CHUNK_BYTES]
            for offset in range(0, len(request), MAX_STREAM_CHUNK_BYTES)
        )
        write_chunk_stream(self._stream, chunks)
        yield from iter_chunk_stream(
            self._stream, maximum_total=MAX_UPLOAD_PACK_RESPONSE_BYTES
        )

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._discovery = None


@dataclass
class BrokerReceivePackClient(BrokerUploadPackClient):
    def _connect(self) -> None:
        if self._stream is not None:
            return
        validate_broker_socket(self.socket_path)
        capability = read_broker_capability(self.capability_path)
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
        stream: BinaryIO | None = None
        try:
            client.settimeout(60)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            request = BrokerRequest(
                version=PROTOCOL_VERSION,
                capability=capability,
                project_id=self.project_id,
                sequence=secrets.randbelow((1 << 63) - 1) + 1,
                operation="git-receive-pack",
                payload={"repository": self.repository},
            )
            stream.write(encode_request_frame(request))
            stream.flush()
            response = read_response_frame(stream)
            if response.status != "ok":
                raise RuntimeError("Git broker request was denied")
            discovery = b"".join(
                iter_chunk_stream(
                    stream, maximum_total=MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES
                )
            )
            if not discovery:
                raise RuntimeError("Git broker discovery failed")
        except Exception:
            if stream is not None:
                stream.close()
            client.close()
            raise
        self._socket = client
        self._stream = stream
        self._discovery = discovery

    def push(self, request: bytes) -> Iterable[bytes]:
        self._connect()
        if self._stream is None:
            raise RuntimeError("Git broker connection failed")
        if not isinstance(request, bytes) or not request:
            raise ValueError("Git broker request is invalid")
        chunks = (
            request[offset : offset + MAX_STREAM_CHUNK_BYTES]
            for offset in range(0, len(request), MAX_STREAM_CHUNK_BYTES)
        )
        write_chunk_stream(self._stream, chunks)
        yield from iter_chunk_stream(
            self._stream, maximum_total=MAX_RECEIVE_PACK_RESPONSE_BYTES
        )


def handle_upload_pack_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubUploadPackTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    try:
        request = initial_request or read_request_frame(connection)
        authorized = session.authorize(request)
        if authorized["operation"] != "git-upload-pack" or authorized["payload"] != {
            "repository": session.policy.repository.slug
        }:
            raise ValueError("broker upload-pack request is not allowed")
    except (ValueError, OSError):
        _write_response(connection, "denied")
        return 1

    try:
        discovery = transport.discover()
    except BrokerStageError as error:
        session.audit(
            operation="git-upload-pack",
            status="error",
            stage=error.stage,
        )
        _write_response(connection, "error")
        return 1

    if not _write_response(connection, "ok"):
        session.audit(
            operation="git-upload-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    try:
        transferred = write_chunk_stream(connection, (discovery,))
    except (ValueError, OSError):
        session.audit(
            operation="git-upload-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    while True:
        try:
            chunks = iter_chunk_stream(
                connection,
                maximum_total=MAX_STATELESS_REQUEST_BYTES,
                allow_initial_eof=True,
            )
            request_body = b"".join(chunks)
        except (ValueError, OSError):
            session.audit(
                operation="git-upload-pack",
                status="error",
                stage="response-stream",
            )
            return 1
        if not request_body:
            session.audit(
                operation="git-upload-pack",
                status="ok",
                bytes_transferred=transferred,
            )
            return 0
        try:
            transferred += write_chunk_stream(connection, transport.rpc(request_body))
        except BrokerStageError as error:
            session.audit(
                operation="git-upload-pack",
                status="error",
                stage=error.stage,
            )
            return 1
        except (ValueError, OSError):
            session.audit(
                operation="git-upload-pack",
                status="error",
                stage="response-stream",
            )
            return 1


def handle_receive_pack_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubReceivePackTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    try:
        request = initial_request or read_request_frame(connection)
        authorized = session.authorize(request)
        if authorized["operation"] != "git-receive-pack" or authorized["payload"] != {
            "repository": session.policy.repository.slug
        }:
            raise ValueError("broker receive-pack request is not allowed")
    except (ValueError, OSError):
        _write_response(connection, "denied")
        return 1

    try:
        discovery = transport.discover()
        advertisement = parse_receive_pack_advertisement(discovery)
    except BrokerStageError as error:
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage=error.stage,
        )
        _write_response(connection, "error")
        return 1
    except ValueError:
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage="receive-discovery",
        )
        _write_response(connection, "error")
        return 1

    if not _write_response(connection, "ok"):
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    try:
        write_chunk_stream(connection, (discovery,))
    except (ValueError, OSError):
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    try:
        request_body = b"".join(
            iter_chunk_stream(
                connection, maximum_total=MAX_RECEIVE_PACK_REQUEST_BYTES
            )
        )
    except (ValueError, OSError):
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    try:
        gated = gate_receive_pack_commands(
            request_body, advertisement, session.policy
        )
    except ValueError:
        session.audit(operation="git-receive-pack", status="denied")
        return 1
    try:
        transferred = write_chunk_stream(connection, transport.rpc(request_body))
    except BrokerStageError as error:
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage=error.stage,
        )
        return 1
    except (ValueError, OSError):
        session.audit(
            operation="git-receive-pack",
            status="error",
            stage="response-stream",
        )
        return 1
    for update in gated.updates:
        session.audit(
            operation="git-receive-pack",
            status="ok",
            ref=update.ref,
            bytes_transferred=transferred,
        )
    return 0


def _validate_pr_payload(
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    if operation == "pr-create" and set(payload) == {
        "base",
        "head",
        "title",
        "body",
    }:
        base, head, title, body = (
            payload["base"], payload["head"], payload["title"], payload["body"]
        )
        if not all(isinstance(value, str) for value in (base, head, title, body)):
            raise ValueError("broker pull request payload is invalid")
        return {"base": base, "head": head, "title": title, "body": body}
    if operation in {"pr-view", "pr-checks"} and set(payload) == {"number"}:
        number = payload["number"]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("broker pull request payload is invalid")
        return {"number": number}
    raise ValueError("broker pull request payload is invalid")


def handle_pull_request_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubPullRequestTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    operation = "pr-view"
    try:
        request = initial_request or read_request_frame(connection)
        operation = request.operation
        authorized = session.authorize(request)
        operation = authorized["operation"]
        if operation not in _PR_OPERATIONS:
            raise ValueError("broker pull request operation is not allowed")
        payload = _validate_pr_payload(operation, authorized["payload"])
    except (ValueError, OSError):
        _write_response(connection, "denied")
        try:
            session.audit(operation=operation, status="denied")
        except ValueError:
            pass
        return 1

    try:
        if operation == "pr-create":
            result = transport.create(**payload)  # type: ignore[arg-type]
        elif operation == "pr-view":
            result = transport.view(payload["number"])  # type: ignore[arg-type]
        else:
            result = transport.checks(payload["number"])  # type: ignore[arg-type]
    except BrokerStageError as error:
        _write_response(connection, "error")
        session.audit(operation=operation, status="error", stage=error.stage)
        return 1

    body = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not body or len(body) > MAX_PR_RESPONSE_BYTES:
        raise ValueError("broker pull request response is invalid")

    if not _write_response(connection, "ok"):
        session.audit(operation=operation, status="error", stage="response-stream")
        return 1
    try:
        transferred = write_chunk_stream(connection, (body,))
    except (ValueError, OSError):
        session.audit(operation=operation, status="error", stage="response-stream")
        return 1
    number = result.get("number")
    session.audit(
        operation=operation,
        status="ok",
        pr_number=(
            number
            if isinstance(number, int) and not isinstance(number, bool)
            else None
        ),
        bytes_transferred=transferred,
    )
    return 0


def _validate_issue_payload(
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    if operation == "issue-list" and not payload:
        return {}
    if operation == "issue-view" and set(payload) == {"number"}:
        number = validate_issue_number(payload["number"])  # type: ignore[arg-type]
        return {"number": number}
    raise ValueError("broker Issue payload is invalid")


def handle_issue_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubIssueTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    operation = "issue-view"
    issue_number: int | None = None
    try:
        request = initial_request or read_request_frame(connection)
        operation = request.operation
        authorized = session.authorize(request)
        operation = authorized["operation"]
        if operation not in _ISSUE_OPERATIONS:
            raise ValueError("broker Issue operation is not allowed")
        payload = _validate_issue_payload(operation, authorized["payload"])
        if operation == "issue-view":
            issue_number = payload["number"]  # type: ignore[assignment]
    except (ValueError, OSError):
        _write_response(connection, "denied")
        try:
            session.audit(operation=operation, status="denied")
        except ValueError:
            pass
        return 1

    try:
        if operation == "issue-list":
            result = transport.list_open()
        else:
            assert issue_number is not None
            result = transport.view(issue_number)
    except BrokerStageError as error:
        _write_response(connection, "error")
        session.audit(
            operation=operation,
            status="error",
            stage=error.stage,
            issue_number=issue_number,
        )
        return 1

    try:
        body = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        body = b""
    if not body or len(body) > MAX_ISSUE_RESPONSE_BYTES:
        _write_response(connection, "error")
        session.audit(
            operation=operation,
            status="error",
            stage="issue-request",
            issue_number=issue_number,
        )
        return 1

    if not _write_response(connection, "ok"):
        session.audit(
            operation=operation,
            status="error",
            stage="response-stream",
            issue_number=issue_number,
        )
        return 1
    try:
        transferred = write_chunk_stream(connection, (body,))
    except (ValueError, OSError):
        session.audit(
            operation=operation,
            status="error",
            stage="response-stream",
            issue_number=issue_number,
        )
        return 1
    session.audit(
        operation=operation,
        status="ok",
        issue_number=issue_number,
        bytes_transferred=transferred,
    )
    return 0


def handle_broker_connection(
    session: BrokerSession,
    connection: BinaryIO,
    upload_transport: GitHubUploadPackTransport,
    receive_transport: GitHubReceivePackTransport | None,
    pr_transport: GitHubPullRequestTransport | None = None,
    issue_transport: GitHubIssueTransport | None = None,
) -> int:
    try:
        request = read_request_frame(connection)
    except (ValueError, OSError):
        _write_response(connection, "denied")
        return 1
    if request.operation == "git-upload-pack":
        return handle_upload_pack_connection(
            session, connection, upload_transport, request
        )
    if request.operation == "git-receive-pack" and receive_transport is not None:
        return handle_receive_pack_connection(
            session, connection, receive_transport, request
        )
    if request.operation in _PR_OPERATIONS and pr_transport is not None:
        return handle_pull_request_connection(
            session, connection, pr_transport, request
        )
    if request.operation in _ISSUE_OPERATIONS and issue_transport is not None:
        return handle_issue_connection(
            session, connection, issue_transport, request
        )
    _write_response(connection, "denied")
    return 1
