from dataclasses import dataclass
import json
import struct
from typing import Any


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
_HEADER_BYTES = 4
_REQUEST_FIELDS = frozenset(
    {"version", "capability", "project_id", "sequence", "operation", "payload"}
)


@dataclass(frozen=True)
class BrokerRequest:
    version: int
    capability: str
    project_id: str
    sequence: int
    operation: str
    payload: dict[str, Any]


def _reject_constant(_: str) -> None:
    raise ValueError("broker request JSON is invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("broker request JSON is invalid")
        result[key] = value
    return result


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
    if not isinstance(data, bytes) or len(data) < _HEADER_BYTES:
        raise ValueError("broker request frame is incomplete")
    length = struct.unpack(">I", data[:_HEADER_BYTES])[0]
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("broker request frame size is invalid")
    consumed = _HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError("broker request frame is incomplete")
    try:
        decoded = json.loads(
            data[_HEADER_BYTES:consumed].decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("broker request JSON is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != _REQUEST_FIELDS:
        raise ValueError("broker request schema is invalid")
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
    if isinstance(sequence, bool) or not isinstance(sequence, int):
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
