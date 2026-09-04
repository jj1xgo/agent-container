from dataclasses import dataclass
import os
from typing import Any
from typing import BinaryIO

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import read_frame


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_DOCUMENT_BYTES = 65_536

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
_REQUEST_OPERATION = "create"
_JSON = JsonOptions(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
_REQUEST_SCHEMA = FrameSchema(
    label="handover broker request",
    stream_label="handover broker stream",
    fields=_REQUEST_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=_JSON,
)
# Responses share the request byte cap; the pre-kernel encoder bounded both on MAX_REQUEST_BYTES.
_RESPONSE_SCHEMA = FrameSchema(
    label="handover broker response",
    stream_label="handover broker stream",
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=_JSON,
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


def _validate_operation(value: Any) -> str:
    operation = _validate_string(value)
    if operation != _REQUEST_OPERATION:
        raise ValueError("handover broker request schema is invalid")
    return operation


def _request_from_values(values: dict[str, Any]) -> HandoverRequest:
    return HandoverRequest(
        version=_validate_version(values["version"]),
        capability=_validate_string(values["capability"]),
        project_id=_validate_string(values["project_id"]),
        operation=_validate_operation(values["operation"]),
        title=_validate_string(values["title"]),
        body=_validate_string(values["body"]),
    )


def encode_request_frame(request: HandoverRequest) -> bytes:
    if not isinstance(request, HandoverRequest):
        raise ValueError("handover broker request is invalid")
    payload_values = {
        "version": _validate_version(request.version),
        "capability": _validate_string(request.capability),
        "project_id": _validate_string(request.project_id),
        "operation": _validate_operation(request.operation),
        "title": _validate_string(request.title),
        "body": _validate_string(request.body),
    }
    return encode_frame(_REQUEST_SCHEMA, payload_values)


def decode_request_frame(data: bytes) -> tuple[HandoverRequest, int]:
    decoded, consumed = decode_frame(_REQUEST_SCHEMA, data)
    return _request_from_values(decoded), consumed


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
    return encode_frame(
        _RESPONSE_SCHEMA,
        {"version": version, "status": status, "path": path, "code": code},
    )


def _response_from_values(values: dict[str, Any]) -> HandoverResponse:
    response = HandoverResponse(
        version=values["version"],
        status=values["status"],
        path=values["path"],
        code=values["code"],
    )
    _validate_response(response)
    return response


def decode_response_frame(data: bytes) -> tuple[HandoverResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    return _response_from_values(decoded), consumed


def read_request_frame(stream: BinaryIO) -> HandoverRequest:
    return _request_from_values(read_frame(_REQUEST_SCHEMA, stream))


def read_response_frame(stream: BinaryIO) -> HandoverResponse:
    return _response_from_values(read_frame(_RESPONSE_SCHEMA, stream))
