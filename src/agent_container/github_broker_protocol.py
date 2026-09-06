from dataclasses import dataclass
import json
from typing import Any
from typing import BinaryIO, Iterable

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import FrameSchemaError
from agent_container.broker.frame import FrameSizeError
from agent_container.broker.frame import HEADER_BYTES
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import iter_chunk_stream as kernel_iter_chunks
from agent_container.broker.frame import read_exact
from agent_container.broker.frame import write_chunk_stream as kernel_write_chunks


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
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

MAX_RESPONSE_BYTES = 1024
_RESPONSE_SCHEMA = FrameSchema(
    label="broker response",
    stream_label="broker stream",
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_RESPONSE_BYTES,
    json=JsonOptions(
        ensure_ascii=True,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
        encoding="ascii",
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
    try:
        return encode_frame(
            _REQUEST_SCHEMA,
            {
                "version": request.version,
                "capability": request.capability,
                "project_id": request.project_id,
                "sequence": request.sequence,
                "operation": request.operation,
                "payload": request.payload,
            },
        )
    except FrameSizeError:
        raise ValueError("broker request is too large") from None


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
    return encode_frame(
        _RESPONSE_SCHEMA, {"version": response.version, "status": response.status}
    )


def decode_response_frame(data: bytes) -> tuple[BrokerResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    version = decoded["version"]
    status = decoded["status"]
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise FrameSchemaError("broker response schema is invalid")
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise FrameSchemaError("broker response schema is invalid")
    return BrokerResponse(version=version, status=status), consumed


def read_request_frame(stream: BinaryIO) -> BrokerRequest:
    header = read_exact(stream, HEADER_BYTES, label="broker stream")
    length = int.from_bytes(header, "big")
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise FrameSizeError("broker request frame size is invalid")
    body = read_exact(stream, length, label="broker stream")
    request, consumed = decode_request_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("broker request frame is invalid")
    return request


def read_response_frame(stream: BinaryIO) -> BrokerResponse:
    header = read_exact(stream, HEADER_BYTES, label="broker stream")
    length = int.from_bytes(header, "big")
    if length == 0 or length > MAX_RESPONSE_BYTES:
        raise FrameSizeError("broker response frame size is invalid")
    body = read_exact(stream, length, label="broker stream")
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
        read_bytes=lambda size, initial: read_exact(
            stream, size, label="broker stream", initial_eof=initial
        ),
        maximum_total=maximum_total,
        maximum_chunk=MAX_STREAM_CHUNK_BYTES,
        label="broker stream",
        allow_initial_eof=allow_initial_eof,
    )
