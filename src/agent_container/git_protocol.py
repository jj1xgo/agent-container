from dataclasses import dataclass
import re

from agent_container.github_broker_policy import BrokerPolicy


ZERO_OID_SHA1 = "0" * 40
ZERO_OID_SHA256 = "0" * 64
MAX_PACKET_BYTES = 65_520
MAX_SECTION_BYTES = 1_048_576
MAX_SECTION_PACKETS = 1_024
MAX_REF_UPDATES = 8
_HEX_HEADER = re.compile(br"^[0-9a-fA-F]{4}$")
_OID_FORMATS = {"sha1": 40, "sha256": 64}
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/{}/-]{0,511}$")
_AGENT = re.compile(r"^agent=[A-Za-z0-9][A-Za-z0-9._/+~-]{0,127}$")
_FIXED_CLIENT_CAPABILITIES = frozenset(
    {
        "report-status",
        "report-status-v2",
        "side-band-64k",
        "quiet",
        "atomic",
        "ofs-delta",
    }
)


@dataclass(frozen=True)
class ReceivePackAdvertisement:
    refs: dict[str, str]
    capabilities: frozenset[str]
    object_format: str
    consumed: int


@dataclass(frozen=True)
class RefUpdate:
    old_oid: str
    new_oid: str
    ref: str


@dataclass(frozen=True)
class ReceivePackGate:
    updates: tuple[RefUpdate, ...]
    capabilities: frozenset[str]
    consumed: int


def decode_pkt_line_section(
    data: bytes,
    *,
    maximum_packet: int = MAX_PACKET_BYTES,
    maximum_section: int = MAX_SECTION_BYTES,
    maximum_packets: int = MAX_SECTION_PACKETS,
) -> tuple[tuple[bytes, ...], int]:
    if not isinstance(data, bytes):
        raise ValueError("pkt-line input is invalid")
    packets: list[bytes] = []
    offset = 0
    while True:
        if offset + 4 > len(data):
            raise ValueError("pkt-line section is incomplete")
        header = data[offset : offset + 4]
        if _HEX_HEADER.fullmatch(header) is None:
            raise ValueError("pkt-line header is invalid")
        length = int(header, 16)
        if length == 0:
            consumed = offset + 4
            if consumed > maximum_section:
                raise ValueError("pkt-line section is too large")
            return tuple(packets), consumed
        if length < 4:
            raise ValueError("pkt-line control packet is not allowed")
        if length > maximum_packet:
            raise ValueError("pkt-line packet is too large")
        end = offset + length
        if end > len(data):
            raise ValueError("pkt-line packet is incomplete")
        if end > maximum_section:
            raise ValueError("pkt-line section is too large")
        if len(packets) >= maximum_packets:
            raise ValueError("pkt-line section has too many packets")
        packets.append(data[offset + 4 : end])
        offset = end


def _without_lf(payload: bytes) -> bytes:
    return payload[:-1] if payload.endswith(b"\n") else payload


def _decode_ascii(payload: bytes, error: str) -> str:
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(error) from None


def _parse_capabilities(raw: bytes) -> tuple[str, ...]:
    text = _decode_ascii(raw, "Git capabilities are invalid")
    capabilities = tuple(text.split(" ")) if text else ()
    if any(not capability for capability in capabilities):
        raise ValueError("Git capabilities are invalid")
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("Git capabilities are invalid")
    names = tuple(capability.split("=", 1)[0] for capability in capabilities)
    if len(set(names)) != len(names):
        raise ValueError("Git capabilities are invalid")
    return capabilities


def _object_format(capabilities: tuple[str, ...]) -> str:
    formats = [
        capability.removeprefix("object-format=")
        for capability in capabilities
        if capability.startswith("object-format=")
    ]
    if not formats:
        return "sha1"
    if len(formats) != 1 or formats[0] not in _OID_FORMATS:
        raise ValueError("Git object format is invalid")
    return formats[0]


def _validate_oid(value: str, object_format: str) -> str:
    expected = _OID_FORMATS[object_format]
    if len(value) != expected or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Git object ID is invalid")
    return value


def _validate_advertised_ref(value: str) -> str:
    if (
        value not in {"HEAD", "capabilities^{}"}
        and (_REF.fullmatch(value) is None or not value.startswith("refs/"))
    ):
        raise ValueError("Git advertisement ref is invalid")
    return value


def parse_receive_pack_advertisement(data: bytes) -> ReceivePackAdvertisement:
    packets, consumed = decode_pkt_line_section(data)
    if not packets:
        raise ValueError("Git advertisement is empty")
    first = _without_lf(packets[0])
    if b"\0" not in first:
        raise ValueError("Git advertisement capabilities are missing")
    first_ref, raw_capabilities = first.split(b"\0", 1)
    capabilities = _parse_capabilities(raw_capabilities)
    object_format = _object_format(capabilities)
    refs: dict[str, str] = {}
    for index, raw_line in enumerate(packets):
        line = first_ref if index == 0 else _without_lf(raw_line)
        if index > 0 and b"\0" in line:
            raise ValueError("Git advertisement is invalid")
        parts = line.split(b" ")
        if len(parts) != 2:
            raise ValueError("Git advertisement is invalid")
        oid = _validate_oid(_decode_ascii(parts[0], "Git advertisement is invalid"), object_format)
        ref = _validate_advertised_ref(
            _decode_ascii(parts[1], "Git advertisement is invalid")
        )
        if ref == "capabilities^{}":
            if oid != "0" * _OID_FORMATS[object_format] or len(packets) != 1:
                raise ValueError("Git empty advertisement is invalid")
            continue
        if ref in refs:
            raise ValueError("Git advertisement has duplicate refs")
        refs[ref] = oid
    return ReceivePackAdvertisement(
        refs=refs,
        capabilities=frozenset(capabilities),
        object_format=object_format,
        consumed=consumed,
    )


def _validate_client_capabilities(
    capabilities: tuple[str, ...], advertisement: ReceivePackAdvertisement
) -> frozenset[str]:
    object_format = advertisement.object_format
    formats = [item for item in capabilities if item.startswith("object-format=")]
    if formats and formats != [f"object-format={object_format}"]:
        raise ValueError("Git object format does not match")
    advertised_names = {
        capability.split("=", 1)[0] for capability in advertisement.capabilities
    }
    for capability in capabilities:
        name = capability.split("=", 1)[0]
        if name not in advertised_names:
            raise ValueError("Git capability is not allowed")
        if capability in _FIXED_CLIENT_CAPABILITIES:
            continue
        if capability == f"object-format={object_format}":
            continue
        if _AGENT.fullmatch(capability) is not None:
            continue
        raise ValueError("Git capability is not allowed")
    return frozenset(capabilities)


def gate_receive_pack_commands(
    data: bytes,
    advertisement: ReceivePackAdvertisement,
    policy: BrokerPolicy,
) -> ReceivePackGate:
    packets, consumed = decode_pkt_line_section(data)
    if not packets:
        raise ValueError("Git push has no ref updates")
    if len(packets) > MAX_REF_UPDATES:
        raise ValueError("Git push has too many ref updates")
    updates: list[RefUpdate] = []
    seen: set[str] = set()
    capabilities: frozenset[str] = frozenset()
    zero_oid = "0" * _OID_FORMATS[advertisement.object_format]
    for index, raw_packet in enumerate(packets):
        line = _without_lf(raw_packet)
        if index == 0:
            if b"\0" not in line:
                raise ValueError("Git push capabilities are missing")
            raw_command, raw_capabilities = line.split(b"\0", 1)
            parsed_capabilities = _parse_capabilities(
                raw_capabilities.removeprefix(b" ")
            )
            capabilities = _validate_client_capabilities(
                parsed_capabilities, advertisement
            )
        else:
            if b"\0" in line:
                raise ValueError("Git push command is invalid")
            raw_command = line
        parts = raw_command.split(b" ")
        if len(parts) != 3:
            raise ValueError("Git push command is invalid")
        old_oid = _validate_oid(
            _decode_ascii(parts[0], "Git push command is invalid"),
            advertisement.object_format,
        )
        new_oid = _validate_oid(
            _decode_ascii(parts[1], "Git push command is invalid"),
            advertisement.object_format,
        )
        ref = _decode_ascii(parts[2], "Git push command is invalid")
        policy.validate_push_ref(ref)
        if ref in seen:
            raise ValueError("Git push contains duplicate refs")
        seen.add(ref)
        if new_oid == zero_oid:
            raise ValueError("Git ref deletion is not allowed")
        advertised_oid = advertisement.refs.get(ref)
        if advertised_oid is None:
            if old_oid != zero_oid:
                raise ValueError("Git push lease does not match")
        elif old_oid != advertised_oid:
            raise ValueError("Git push lease does not match")
        updates.append(RefUpdate(old_oid=old_oid, new_oid=new_oid, ref=ref))
    return ReceivePackGate(
        updates=tuple(updates),
        capabilities=capabilities,
        consumed=consumed,
    )
