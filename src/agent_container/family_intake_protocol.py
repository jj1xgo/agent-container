"""Strict framing for credential-free family Issue intake."""

from dataclasses import dataclass
import json
import struct
from typing import Any, BinaryIO

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.family_issue import parse_family_issue_draft


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_024

_HEADER_BYTES = 4
_REQUEST_FIELDS = frozenset({"version", "operation", "capability", "payload"})
_RESPONSE_FIELDS = frozenset({"version", "status", "request_id", "expires_at"})
_REQUEST_OPERATION = "issue_create_request"
_RESPONSE_STATUS = "pending"


@dataclass(frozen=True)
class FamilyIntakeRequest:
    version: int
    operation: str
    capability: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FamilyIntakeResponse:
    version: int
    status: str
    request_id: str
    expires_at: int


def _reject_constant(_: str) -> None:
    raise ValueError("family intake JSON is invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family intake JSON is invalid")
        result[key] = value
    return result


def _validate_version(value: object) -> int:
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise ValueError("family intake schema is invalid")
    return value


def _validate_text(value: object, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value):
        raise ValueError("family intake schema is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("family intake schema is invalid") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("family intake schema is invalid")
    return value


def _validate_request(request: object) -> FamilyIntakeRequest:
    if type(request) is not FamilyIntakeRequest:
        raise ValueError("family intake request is invalid")
    version = _validate_version(request.version)
    operation = _validate_text(request.operation)
    capability = _validate_text(request.capability)
    if operation != _REQUEST_OPERATION or type(request.payload) is not dict:
        raise ValueError("family intake request schema is invalid")
    draft = parse_family_issue_draft(request.payload)
    payload = {
        "title": draft.title,
        "summary": draft.summary,
        "context": draft.context,
        "acceptance_criteria": list(draft.acceptance_criteria),
    }
    return FamilyIntakeRequest(version, operation, capability, payload)


def _validate_response(response: object) -> FamilyIntakeResponse:
    if type(response) is not FamilyIntakeResponse:
        raise ValueError("family intake response is invalid")
    version = _validate_version(response.version)
    status = _validate_text(response.status)
    request_id = _validate_text(response.request_id)
    if (
        status != _RESPONSE_STATUS
        or len(request_id) > 128
        or type(response.expires_at) is not int
        or response.expires_at < 1
    ):
        raise ValueError("family intake response schema is invalid")
    return FamilyIntakeResponse(version, status, request_id, response.expires_at)


def _frame_schema(
    *, maximum: int, kind: str, fields: frozenset[str] = frozenset(),
) -> FrameSchema:
    return FrameSchema(
        label=f"family intake {kind}",
        stream_label="family intake stream",
        fields=fields,
        max_bytes=maximum - _HEADER_BYTES,
        json=JsonOptions(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _encode(values: dict[str, Any], *, maximum: int, kind: str) -> bytes:
    return encode_frame(_frame_schema(maximum=maximum, kind=kind), values)


def encode_request_frame(request: FamilyIntakeRequest) -> bytes:
    request = _validate_request(request)
    return _encode(
        {
            "version": request.version,
            "operation": request.operation,
            "capability": request.capability,
            "payload": request.payload,
        },
        maximum=MAX_REQUEST_BYTES,
        kind="request",
    )


def encode_response_frame(response: FamilyIntakeResponse) -> bytes:
    response = _validate_response(response)
    return _encode(
        {
            "version": response.version,
            "status": response.status,
            "request_id": response.request_id,
            "expires_at": response.expires_at,
        },
        maximum=MAX_RESPONSE_BYTES,
        kind="response",
    )


def _frame_body(data: bytes, *, maximum: int, kind: str) -> tuple[bytes, int]:
    if type(data) is not bytes or len(data) < _HEADER_BYTES:
        raise ValueError(f"family intake {kind} frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > maximum - _HEADER_BYTES:
        raise ValueError(f"family intake {kind} frame size is invalid")
    consumed = _HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError(f"family intake {kind} frame is incomplete")
    return data[_HEADER_BYTES:consumed], consumed


def _decode_json(body: bytes, kind: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"family intake {kind} JSON is invalid") from None
    if type(decoded) is not dict:
        raise ValueError(f"family intake {kind} schema is invalid")
    return decoded


def _decode_frame(
    data: bytes, *, maximum: int, kind: str, fields: frozenset[str],
) -> tuple[dict[str, Any], int]:
    # Family rejects bytes subclasses; the generic kernel accepts them.
    if type(data) is not bytes:
        raise ValueError(f"family intake {kind} frame is incomplete")
    return decode_frame(
        _frame_schema(maximum=maximum, kind=kind, fields=fields),
        data,
        json_decoder=lambda body: _decode_json(body, kind),
    )


def decode_request_frame(data: bytes) -> tuple[FamilyIntakeRequest, int]:
    decoded, consumed = _decode_frame(
        data, maximum=MAX_REQUEST_BYTES, kind="request", fields=_REQUEST_FIELDS,
    )
    request = FamilyIntakeRequest(
        version=decoded["version"],
        operation=decoded["operation"],
        capability=decoded["capability"],
        payload=decoded["payload"],
    )
    return _validate_request(request), consumed


def decode_response_frame(data: bytes) -> tuple[FamilyIntakeResponse, int]:
    decoded, consumed = _decode_frame(
        data, maximum=MAX_RESPONSE_BYTES, kind="response", fields=_RESPONSE_FIELDS,
    )
    response = FamilyIntakeResponse(
        version=decoded["version"],
        status=decoded["status"],
        request_id=decoded["request_id"],
        expires_at=decoded["expires_at"],
    )
    return _validate_response(response), consumed


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise ValueError("family intake stream is invalid") from None
        if (
            type(chunk) is not bytes
            or not chunk
            or len(chunk) > size - len(output)
        ):
            raise ValueError("family intake stream is incomplete")
        output.extend(chunk)
    return bytes(output)


def _read_frame(stream: BinaryIO, *, maximum: int, kind: str) -> bytes:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > maximum - _HEADER_BYTES:
        raise ValueError(f"family intake {kind} frame size is invalid")
    return header + _read_exact(stream, length)


def read_request_frame(stream: BinaryIO) -> FamilyIntakeRequest:
    request, _ = decode_request_frame(
        _read_frame(stream, maximum=MAX_REQUEST_BYTES, kind="request")
    )
    return request


def read_response_frame(stream: BinaryIO) -> FamilyIntakeResponse:
    response, _ = decode_response_frame(
        _read_frame(stream, maximum=MAX_RESPONSE_BYTES, kind="response")
    )
    return response


def _write_all(stream: BinaryIO, frame: bytes) -> None:
    offset = 0
    while offset < len(frame):
        try:
            written = stream.write(frame[offset:])
        except (OSError, TypeError, ValueError):
            raise ValueError("family intake stream is invalid") from None
        if (
            type(written) is not int
            or written < 1
            or written > len(frame) - offset
        ):
            raise ValueError("family intake stream write failed")
        offset += written
    try:
        stream.flush()
    except (OSError, TypeError, ValueError):
        raise ValueError("family intake stream is invalid") from None


def write_request_frame(stream: BinaryIO, request: FamilyIntakeRequest) -> None:
    _write_all(stream, encode_request_frame(request))


def write_response_frame(stream: BinaryIO, response: FamilyIntakeResponse) -> None:
    _write_all(stream, encode_response_frame(response))
