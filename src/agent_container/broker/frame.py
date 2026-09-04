"""Length-prefixed JSON frames shared by every broker protocol."""

from dataclasses import dataclass
import json
import struct
from typing import Any
from typing import BinaryIO


HEADER_BYTES = 4


@dataclass(frozen=True)
class JsonOptions:
    ensure_ascii: bool = True
    allow_nan: bool = True
    sort_keys: bool = False
    separators: tuple[str, str] | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True)
class FrameSchema:
    label: str
    stream_label: str
    fields: frozenset[str]
    max_bytes: int
    json: JsonOptions


def _reject_constant(_: str) -> None:
    raise ValueError("JSON constant is not allowed")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object has a duplicate key")
        result[key] = value
    return result


def encode_frame(schema: FrameSchema, values: dict[str, Any]) -> bytes:
    options = schema.json
    try:
        body = json.dumps(
            values,
            ensure_ascii=options.ensure_ascii,
            allow_nan=options.allow_nan,
            sort_keys=options.sort_keys,
            separators=options.separators,
        ).encode(options.encoding)
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ValueError(f"{schema.label} is invalid") from None
    if not body or len(body) > schema.max_bytes:
        raise ValueError(f"{schema.label} is too large")
    return struct.pack(">I", len(body)) + body


def decode_frame(schema: FrameSchema, data: bytes) -> tuple[dict[str, Any], int]:
    if not isinstance(data, bytes) or len(data) < HEADER_BYTES:
        raise ValueError(f"{schema.label} frame is incomplete")
    length = struct.unpack(">I", data[:HEADER_BYTES])[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.label} frame size is invalid")
    consumed = HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError(f"{schema.label} frame is incomplete")
    try:
        text = data[HEADER_BYTES:consumed].decode(schema.json.encoding)
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"{schema.label} JSON is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != schema.fields:
        raise ValueError(f"{schema.label} schema is invalid")
    return decoded, consumed


def read_exact(stream: BinaryIO, size: int, *, label: str) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise ValueError(f"{label} is invalid") from None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(output):
            raise ValueError(f"{label} is incomplete")
        output.extend(chunk)
    return bytes(output)


def read_frame(schema: FrameSchema, stream: BinaryIO) -> dict[str, Any]:
    header = read_exact(stream, HEADER_BYTES, label=schema.stream_label)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.label} frame size is invalid")
    body = read_exact(stream, length, label=schema.stream_label)
    decoded, consumed = decode_frame(schema, header + body)
    if consumed != len(header) + len(body):
        raise ValueError(f"{schema.label} frame is invalid")
    return decoded


def write_all(stream: BinaryIO, frame: bytes, *, label: str) -> None:
    offset = 0
    while offset < len(frame):
        written = stream.write(frame[offset:])
        if (
            isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
            or written > len(frame) - offset
        ):
            raise ValueError(f"{label} write failed")
        offset += written
    stream.flush()
