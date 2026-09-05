from dataclasses import dataclass
from typing import Any
from typing import BinaryIO

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import read_frame
from agent_container.egress_policy import validate_domain


PROTOCOL_VERSION = 1
MAX_METADATA_BYTES = 16_384
MAX_SEQUENCE = (1 << 63) - 1
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
_FRAME_LABEL = "egress metadata"
_STREAM_LABEL = "egress metadata stream"
_REQUEST_SCHEMA = FrameSchema(
    label="egress request",
    stream_label=_STREAM_LABEL,
    fields=_REQUEST_FIELDS,
    max_bytes=MAX_METADATA_BYTES,
    json=JsonOptions(allow_nan=False, sort_keys=True, separators=(",", ":"), encoding="ascii"),
    frame_label=_FRAME_LABEL,
)
# The pre-kernel encoder put no byte cap on responses; the fixed status/code
# vocabulary keeps every response far below the request cap reused here.
_RESPONSE_SCHEMA = FrameSchema(
    label="egress response",
    stream_label=_STREAM_LABEL,
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_METADATA_BYTES,
    json=JsonOptions(sort_keys=True, separators=(",", ":"), encoding="ascii"),
    frame_label=_FRAME_LABEL,
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


def _request_from_values(values: dict[str, Any]) -> EgressRequest:
    request = EgressRequest(**values)
    _validate_request(request)
    return request


def encode_request_frame(request: EgressRequest) -> bytes:
    if not isinstance(request, EgressRequest):
        raise ValueError("egress request is invalid")
    _validate_request(request)
    return encode_frame(
        _REQUEST_SCHEMA,
        {
            "version": request.version,
            "capability": request.capability,
            "project_id": request.project_id,
            "sequence": request.sequence,
            "operation": request.operation,
            "domain": request.domain,
            "port": request.port,
        },
    )


def decode_request_frame(data: bytes) -> tuple[EgressRequest, int]:
    decoded, consumed = decode_frame(_REQUEST_SCHEMA, data)
    return _request_from_values(decoded), consumed


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


def _response_from_values(values: dict[str, Any]) -> EgressResponse:
    response = EgressResponse(**values)
    _validate_response(response)
    return response


def encode_response_frame(response: EgressResponse) -> bytes:
    if not isinstance(response, EgressResponse):
        raise ValueError("egress response is invalid")
    _validate_response(response)
    return encode_frame(
        _RESPONSE_SCHEMA,
        {
            "version": response.version,
            "status": response.status,
            "code": response.code,
        },
    )


def decode_response_frame(data: bytes) -> tuple[EgressResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    return _response_from_values(decoded), consumed


def read_request_frame(stream: BinaryIO) -> EgressRequest:
    return _request_from_values(read_frame(_REQUEST_SCHEMA, stream))


def read_response_frame(stream: BinaryIO) -> EgressResponse:
    return _response_from_values(read_frame(_RESPONSE_SCHEMA, stream))
