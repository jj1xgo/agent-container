from dataclasses import dataclass
import json
import struct
from typing import Any
from typing import BinaryIO, Iterable

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import iter_chunk_stream as kernel_iter_chunks
from agent_container.broker.frame import write_chunk_stream as kernel_write_chunks


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
_HEADER_BYTES = 4
_REQUEST_FIELDS = frozenset(
    {"version", "capability", "project_id", "sequence", "operation", "payload"}
)
_RESPONSE_FIELDS = frozenset({"version", "status"})
_RESPONSE_STATUSES = frozenset({"ok", "denied", "error"})
MAX_STREAM_CHUNK_BYTES = 1_048_576
MAX_REQUEST_NONCE = (1 << 63) - 1
_REQUEST_SCHEMA = FrameSchema(
    label="broker request",
    stream_label="broker stream",
    fields=_REQUEST_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=JsonOptions(
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        encoding="utf-8",
    ),
)


@dataclass(frozen=True)
class BrokerRequest:
    version: int
    capability: str
    project_id: str
    sequence: int
    operation: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class BrokerResponse:
    version: int
    status: str


def _reject_constant(_: str) -> None:
    raise ValueError("broker request JSON is invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("broker request JSON is invalid")
        result[key] = value
    return result


def _decode_request_json(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("broker request JSON is invalid") from None


def encode_request_frame(request: BrokerRequest) -> bytes:
    payload = json.dumps(
        {
            "version": request.version,
            "capability": request.capability,
            "project_id": request.project_id,
            "sequence": request.sequence,
            "operation": request.operation,
            "payload": request.payload,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("broker request is too large")
    return struct.pack(">I", len(payload)) + payload


def decode_request_frame(data: bytes) -> tuple[BrokerRequest, int]:
    decoded, consumed = decode_frame(
        _REQUEST_SCHEMA, data, json_decoder=_decode_request_json
    )
    version = decoded["version"]
    capability = decoded["capability"]
    project_id = decoded["project_id"]
    sequence = decoded["sequence"]
    operation = decoded["operation"]
    payload = decoded["payload"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("broker request schema is invalid")
    if not isinstance(capability, str) or not isinstance(project_id, str):
        raise ValueError("broker request schema is invalid")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_REQUEST_NONCE
    ):
        raise ValueError("broker request schema is invalid")
    if not isinstance(operation, str) or not isinstance(payload, dict):
        raise ValueError("broker request schema is invalid")
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("broker request schema is invalid")
    return (
        BrokerRequest(
            version=version,
            capability=capability,
            project_id=project_id,
            sequence=sequence,
            operation=operation,
            payload=payload,
        ),
        consumed,
    )


def encode_response_frame(response: BrokerResponse) -> bytes:
    if response.version != PROTOCOL_VERSION or response.status not in _RESPONSE_STATUSES:
        raise ValueError("broker response is invalid")
    payload = json.dumps(
        {"version": response.version, "status": response.status},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return struct.pack(">I", len(payload)) + payload


def decode_response_frame(data: bytes) -> tuple[BrokerResponse, int]:
    if not isinstance(data, bytes) or len(data) < _HEADER_BYTES:
        raise ValueError("broker response frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > 1024 or len(data) < _HEADER_BYTES + length:
        raise ValueError("broker response frame is invalid")
    consumed = _HEADER_BYTES + length
    try:
        payload = json.loads(
            data[_HEADER_BYTES:consumed].decode("ascii"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("broker response frame is invalid") from None
    if not isinstance(payload, dict) or set(payload) != _RESPONSE_FIELDS:
        raise ValueError("broker response schema is invalid")
    version = payload["version"]
    status = payload["status"]
    if version != PROTOCOL_VERSION or status not in _RESPONSE_STATUSES:
        raise ValueError("broker response schema is invalid")
    return BrokerResponse(version=version, status=status), consumed


def _read_exact(stream: BinaryIO, size: int, *, initial_eof: bool = False) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.read(size - len(output))
        if chunk == b"":
            if initial_eof and not output:
                return b""
            raise ValueError("broker stream is incomplete")
        output.extend(chunk)
    return bytes(output)


def read_request_frame(stream: BinaryIO) -> BrokerRequest:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("broker request frame size is invalid")
    body = _read_exact(stream, length)
    request, consumed = decode_request_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("broker request frame is invalid")
    return request


def read_response_frame(stream: BinaryIO) -> BrokerResponse:
    header = _read_exact(stream, _HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > 1024:
        raise ValueError("broker response frame is invalid")
    body = _read_exact(stream, length)
    response, consumed = decode_response_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("broker response frame is invalid")
    return response


def write_chunk_stream(stream: BinaryIO, chunks: Iterable[bytes]) -> int:
    return kernel_write_chunks(
        stream,
        chunks,
        maximum_chunk=MAX_STREAM_CHUNK_BYTES,
        label="broker stream",
    )


def iter_chunk_stream(
    stream: BinaryIO, *, maximum_total: int, allow_initial_eof: bool = False
) -> Iterable[bytes]:
    yield from kernel_iter_chunks(
        read_bytes=lambda size, initial: _read_exact(
            stream, size, initial_eof=initial
        ),
        maximum_total=maximum_total,
        maximum_chunk=MAX_STREAM_CHUNK_BYTES,
        label="broker stream",
        allow_initial_eof=allow_initial_eof,
    )
