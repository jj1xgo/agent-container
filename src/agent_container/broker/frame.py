"""Length-prefixed JSON frames shared by every broker protocol."""

from dataclasses import dataclass
import json
import struct
from typing import Any
from typing import BinaryIO, Callable, Iterable


HEADER_BYTES = 4


class FrameError(ValueError):
    """Every framing failure. Messages are unchanged from stage 1; only the class is specific."""


class FrameIncomplete(FrameError):
    """Fewer bytes than the header or the announced body."""


class FrameSizeError(FrameError):
    """A zero, oversized, or otherwise unacceptable length."""


class FrameJsonError(FrameError):
    """The body is not the accepted JSON subset."""


class FrameSchemaError(FrameError):
    """The decoded object or the values to encode do not match the schema."""


class StreamError(FrameError):
    """Reading from or writing to the underlying stream failed."""


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
    frame_label: str | None = None

    @property
    def frame_prefix(self) -> str:
        return self.label if self.frame_label is None else self.frame_label


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
        raise FrameSchemaError(f"{schema.label} is invalid") from None
    if not body or len(body) > schema.max_bytes:
        raise FrameSizeError(f"{schema.label} is too large")
    return struct.pack(">I", len(body)) + body


def decode_frame(
    schema: FrameSchema,
    data: bytes,
    *,
    json_decoder: Callable[[bytes], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    if not isinstance(data, bytes) or len(data) < HEADER_BYTES:
        raise FrameIncomplete(f"{schema.frame_prefix} frame is incomplete")
    length = struct.unpack(">I", data[:HEADER_BYTES])[0]
    if length == 0 or length > schema.max_bytes:
        raise FrameSizeError(f"{schema.frame_prefix} frame size is invalid")
    consumed = HEADER_BYTES + length
    if len(data) < consumed:
        raise FrameIncomplete(f"{schema.frame_prefix} frame is incomplete")
    if json_decoder is not None:
        decoded = json_decoder(data[HEADER_BYTES:consumed])
    else:
        try:
            text = data[HEADER_BYTES:consumed].decode(schema.json.encoding)
            decoded = json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise FrameJsonError(f"{schema.frame_prefix} JSON is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != schema.fields:
        raise FrameSchemaError(f"{schema.label} schema is invalid")
    return decoded, consumed


def read_exact(
    stream: BinaryIO, size: int, *, label: str, initial_eof: bool = False
) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise StreamError(f"{label} is invalid") from None
        if not isinstance(chunk, bytes):
            raise StreamError(f"{label} is incomplete")
        if not chunk:
            if initial_eof and not output:
                return b""
            raise StreamError(f"{label} is incomplete")
        if len(chunk) > size - len(output):
            raise StreamError(f"{label} is incomplete")
        output.extend(chunk)
    return bytes(output)


def read_frame(schema: FrameSchema, stream: BinaryIO) -> dict[str, Any]:
    header = read_exact(stream, HEADER_BYTES, label=schema.stream_label)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > schema.max_bytes:
        raise FrameSizeError(f"{schema.frame_prefix} frame size is invalid")
    body = read_exact(stream, length, label=schema.stream_label)
    decoded, consumed = decode_frame(schema, header + body)
    if consumed != len(header) + len(body):
        raise FrameError(f"{schema.frame_prefix} frame is invalid")
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
            raise StreamError(f"{label} write failed")
        offset += written
    stream.flush()



def write_chunk_stream(
    stream: BinaryIO, chunks: Iterable[bytes], *, maximum_chunk: int, label: str
) -> int:
    transferred = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > maximum_chunk:
            raise FrameSizeError(f"{label} chunk is invalid")
        stream.write(struct.pack(">I", len(chunk)))
        stream.write(chunk)
        transferred += len(chunk)
    stream.write(b"\x00\x00\x00\x00")
    stream.flush()
    return transferred


def iter_chunk_stream(
    *,
    read_bytes: Callable[[int, bool], bytes],
    maximum_total: int,
    maximum_chunk: int,
    label: str,
    allow_initial_eof: bool = False,
) -> Iterable[bytes]:
    if maximum_total < 0:
        raise ValueError(f"{label} limit is invalid")
    transferred = 0
    while True:
        header = read_bytes(HEADER_BYTES, allow_initial_eof and transferred == 0)
        if header == b"":
            return
        length = struct.unpack(">I", header)[0]
        if length == 0:
            return
        if length > maximum_chunk:
            raise FrameSizeError(f"{label} chunk is invalid")
        transferred += length
        if transferred > maximum_total:
            raise FrameSizeError(f"{label} is too large")
        yield read_bytes(length, False)
