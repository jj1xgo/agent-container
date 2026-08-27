from dataclasses import dataclass
import json
import os
import struct
from typing import Any
from typing import BinaryIO


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_DOCUMENT_BYTES = 65_536

_HEADER_BYTES = 4
_REQUEST_FIELDS = frozenset(
    {"version", "capability", "project_id", "operation", "title", "body"}
)
_RESPONSE_FIELDS = frozenset({"version", "status", "path", "code"})
_RESPONSE_STATUSES = frozenset({"ok", "denied", "error"})
_RESPONSE_CODES = frozenset(
    {
        "authentication",
        "schema",
        "size",
        "content-policy",
        "filesystem-boundary",
        "write",
        "unavailable",
    }
)


@dataclass(frozen=True)
class HandoverRequest:
    version: int
    capability: str
    project_id: str
    operation: str
    title: str
    body: str


@dataclass(frozen=True)
class HandoverResponse:
    version: int
    status: str
    path: str
    code: str


def _reject_constant(_: str) -> None:
    raise ValueError("handover broker JSON is invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("handover broker JSON is invalid")
        result[key] = value
    return result


def _validate_string(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("handover broker schema is invalid")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("handover broker schema is invalid")
    return value


def _validate_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != PROTOCOL_VERSION:
        raise ValueError("handover broker schema is invalid")
    return value


def _decode_json(data: bytes, kind: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"handover broker {kind} JSON is invalid") from None
    if not isinstance(decoded, dict):
        raise ValueError(f"handover broker {kind} schema is invalid")
    return decoded


def encode_request_frame(request: HandoverRequest) -> bytes:
    if not isinstance(request, HandoverRequest):
        raise ValueError("handover broker request is invalid")
    payload_values = {
        "version": _validate_version(request.version),
        "capability": _validate_string(request.capability),
        "project_id": _validate_string(request.project_id),
        "operation": _validate_string(request.operation),
        "title": _validate_string(request.title),
        "body": _validate_string(request.body),
    }
    try:
        payload = json.dumps(
            payload_values,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ValueError("handover broker request is invalid") from None
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("handover broker request is too large")
    return struct.pack(">I", len(payload)) + payload


def decode_request_frame(data: bytes) -> tuple[HandoverRequest, int]:
    if not isinstance(data, bytes) or len(data) < _HEADER_BYTES:
        raise ValueError("handover broker request frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("handover broker request frame size is invalid")
    consumed = _HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError("handover broker request frame is incomplete")
    decoded = _decode_json(data[_HEADER_BYTES:consumed], "request")
    if set(decoded) != _REQUEST_FIELDS:
        raise ValueError("handover broker request schema is invalid")
    version = _validate_version(decoded["version"])
    capability = _validate_string(decoded["capability"])
    project_id = _validate_string(decoded["project_id"])
    operation = _validate_string(decoded["operation"])
    title = _validate_string(decoded["title"])
    body = _validate_string(decoded["body"])
    return (
        HandoverRequest(
            version=version,
            capability=capability,
            project_id=project_id,
            operation=operation,
            title=title,
            body=body,
        ),
        consumed,
    )


def _validate_response(response: HandoverResponse) -> tuple[int, str, str, str]:
    if not isinstance(response, HandoverResponse):
        raise ValueError("handover broker response is invalid")
    version = _validate_version(response.version)
    status = _validate_string(response.status)
    path = _validate_string(response.path)
    code = _validate_string(response.code)
    if status not in _RESPONSE_STATUSES:
        raise ValueError("handover broker response schema is invalid")
    if status == "ok":
        if not path or not os.path.isabs(path) or code:
            raise ValueError("handover broker response schema is invalid")
    elif path or code not in _RESPONSE_CODES:
        raise ValueError("handover broker response schema is invalid")
    return version, status, path, code


def encode_response_frame(response: HandoverResponse) -> bytes:
    version, status, path, code = _validate_response(response)
    try:
        payload = json.dumps(
            {"version": version, "status": status, "path": path, "code": code},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ValueError("handover broker response is invalid") from None
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("handover broker response is too large")
    return struct.pack(">I", len(payload)) + payload


def decode_response_frame(data: bytes) -> tuple[HandoverResponse, int]:
    if not isinstance(data, bytes) or len(data) < _HEADER_BYTES:
        raise ValueError("handover broker response frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("handover broker response frame size is invalid")
    consumed = _HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError("handover broker response frame is incomplete")
    decoded = _decode_json(data[_HEADER_BYTES:consumed], "response")
    if set(decoded) != _RESPONSE_FIELDS:
        raise ValueError("handover broker response schema is invalid")
    response = HandoverResponse(
        version=decoded["version"],
        status=decoded["status"],
        path=decoded["path"],
        code=decoded["code"],
    )
    _validate_response(response)
    return response, consumed


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise ValueError("handover broker stream is invalid") from None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(output):
            raise ValueError("handover broker stream is incomplete")
        output.extend(chunk)
    return bytes(output)


def read_request_frame(stream: BinaryIO) -> HandoverRequest:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("handover broker request frame size is invalid")
    body = _read_exact(stream, length)
    request, consumed = decode_request_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("handover broker request frame is invalid")
    return request


def read_response_frame(stream: BinaryIO) -> HandoverResponse:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("handover broker response frame size is invalid")
    body = _read_exact(stream, length)
    response, consumed = decode_response_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("handover broker response frame is invalid")
    return response
