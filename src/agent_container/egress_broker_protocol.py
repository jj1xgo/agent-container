from dataclasses import dataclass
import json
import struct
from typing import Any
from typing import BinaryIO

from agent_container.egress_policy import validate_domain


PROTOCOL_VERSION = 1
MAX_METADATA_BYTES = 16_384
MAX_SEQUENCE = (1 << 63) - 1
_HEADER_BYTES = 4
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "capability",
        "project_id",
        "sequence",
        "operation",
        "domain",
        "port",
    }
)
_RESPONSE_FIELDS = frozenset({"version", "status", "code"})
_RESPONSE_STATUSES = frozenset({"ok", "denied", "error"})
_RESPONSE_CODES = frozenset(
    {
        "authentication",
        "policy",
        "resolve",
        "connect",
        "limit",
        "relay",
        "unavailable",
    }
)


@dataclass(frozen=True)
class EgressRequest:
    version: int
    capability: str
    project_id: str
    sequence: int
    operation: str
    domain: str
    port: int


@dataclass(frozen=True)
class EgressResponse:
    version: int
    status: str
    code: str


def _reject_constant(_: str) -> None:
    raise ValueError("egress metadata JSON is invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("egress metadata JSON is invalid")
        result[key] = value
    return result


def _validate_request(request: EgressRequest) -> None:
    if (
        isinstance(request.version, bool)
        or not isinstance(request.version, int)
        or request.version != PROTOCOL_VERSION
    ):
        raise ValueError("egress request schema is invalid")
    if (
        not isinstance(request.capability, str)
        or not request.capability
        or not isinstance(request.project_id, str)
        or not request.project_id
    ):
        raise ValueError("egress request schema is invalid")
    if (
        isinstance(request.sequence, bool)
        or not isinstance(request.sequence, int)
        or not 1 <= request.sequence <= MAX_SEQUENCE
    ):
        raise ValueError("egress request schema is invalid")
    if request.operation != "connect":
        raise ValueError("egress request schema is invalid")
    validate_domain(request.domain)
    if (
        isinstance(request.port, bool)
        or not isinstance(request.port, int)
        or request.port != 443
    ):
        raise ValueError("egress request schema is invalid")


def encode_request_frame(request: EgressRequest) -> bytes:
    if not isinstance(request, EgressRequest):
        raise ValueError("egress request is invalid")
    _validate_request(request)
    payload = json.dumps(
        {
            "version": request.version,
            "capability": request.capability,
            "project_id": request.project_id,
            "sequence": request.sequence,
            "operation": request.operation,
            "domain": request.domain,
            "port": request.port,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not payload or len(payload) > MAX_METADATA_BYTES:
        raise ValueError("egress request is too large")
    return struct.pack(">I", len(payload)) + payload


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("ascii"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("egress metadata JSON is invalid") from None


def decode_request_frame(data: bytes) -> tuple[EgressRequest, int]:
    body, consumed = _frame_body(data)
    decoded = _decode_json(body)
    if not isinstance(decoded, dict) or set(decoded) != _REQUEST_FIELDS:
        raise ValueError("egress request schema is invalid")
    request = EgressRequest(**decoded)
    _validate_request(request)
    return request, consumed


def _validate_response(response: EgressResponse) -> None:
    if (
        isinstance(response.version, bool)
        or not isinstance(response.version, int)
        or response.version != PROTOCOL_VERSION
        or not isinstance(response.status, str)
        or response.status not in _RESPONSE_STATUSES
        or not isinstance(response.code, str)
        or response.code not in _RESPONSE_CODES
    ):
        raise ValueError("egress response schema is invalid")


def encode_response_frame(response: EgressResponse) -> bytes:
    if not isinstance(response, EgressResponse):
        raise ValueError("egress response is invalid")
    _validate_response(response)
    payload = json.dumps(
        {
            "version": response.version,
            "status": response.status,
            "code": response.code,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return struct.pack(">I", len(payload)) + payload


def decode_response_frame(data: bytes) -> tuple[EgressResponse, int]:
    body, consumed = _frame_body(data)
    decoded = _decode_json(body)
    if not isinstance(decoded, dict) or set(decoded) != _RESPONSE_FIELDS:
        raise ValueError("egress response schema is invalid")
    response = EgressResponse(**decoded)
    _validate_response(response)
    return response, consumed


def _frame_body(data: bytes) -> tuple[bytes, int]:
    if not isinstance(data, bytes) or len(data) < _HEADER_BYTES:
        raise ValueError("egress metadata frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > MAX_METADATA_BYTES:
        raise ValueError("egress metadata frame size is invalid")
    consumed = _HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError("egress metadata frame is incomplete")
    return data[_HEADER_BYTES:consumed], consumed


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.read(size - len(output))
        if chunk == b"":
            raise ValueError("egress metadata stream is incomplete")
        output.extend(chunk)
    return bytes(output)


def _read_frame(stream: BinaryIO) -> bytes:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_METADATA_BYTES:
        raise ValueError("egress metadata frame size is invalid")
    return header + _read_exact(stream, length)


def read_request_frame(stream: BinaryIO) -> EgressRequest:
    request, _ = decode_request_frame(_read_frame(stream))
    return request


def read_response_frame(stream: BinaryIO) -> EgressResponse:
    response, _ = decode_response_frame(_read_frame(stream))
    return response
