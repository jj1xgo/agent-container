from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import sys
import threading
from typing import Callable
from typing import Mapping
from typing import Sequence

from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import MAX_SEQUENCE
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_broker_protocol import read_response_frame
from agent_container.egress_gateway import RelayCounts
from agent_container.egress_gateway import RelayLimits
from agent_container.egress_gateway import relay_tunnel
from agent_container.egress_policy import validate_domain
from agent_container.state import validate_agent
from agent_container.state import validate_project_id


MAX_CONNECT_HEADER_BYTES = 16_384
CONNECTED_RESPONSE = b"HTTP/1.1 200 Connection Established\r\n\r\n"
BAD_GATEWAY_RESPONSE = b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
_REQUEST_LINE = re.compile(r"CONNECT ([a-z0-9.-]+):443 HTTP/1\.1")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{43}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class AdapterConfig:
    socket_path: Path
    capability: str = field(repr=False)
    project_id: str = ""
    agent: str = ""


@dataclass
class EgressSequence:
    _value: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def next(self) -> int:
        with self._lock:
            if self._value >= MAX_SEQUENCE:
                raise ValueError("egress sequence is exhausted")
            self._value += 1
            return self._value


def _invalid() -> ValueError:
    return ValueError("egress CONNECT request is invalid")


def parse_connect_request(header: bytes) -> str:
    if (
        not isinstance(header, bytes)
        or not header
        or len(header) > MAX_CONNECT_HEADER_BYTES
        or not header.endswith(b"\r\n\r\n")
        or b"\n" in header.replace(b"\r\n", b"")
        or b"\r" in header.replace(b"\r\n", b"")
    ):
        raise _invalid()
    try:
        text = header.decode("ascii")
    except UnicodeDecodeError:
        raise _invalid() from None
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in text.replace("\r\n", "")):
        raise _invalid()
    lines = text[:-4].split("\r\n")
    matched = _REQUEST_LINE.fullmatch(lines[0])
    if matched is None:
        raise _invalid()
    domain = matched.group(1)
    try:
        validate_domain(domain)
    except ValueError:
        raise _invalid() from None

    hosts: list[str] = []
    for line in lines[1:]:
        if ":" not in line:
            raise _invalid()
        name, value = line.split(":", 1)
        if _HEADER_NAME.fullmatch(name) is None or not value.startswith(" "):
            raise _invalid()
        value = value[1:]
        if not value or value != value.strip():
            raise _invalid()
        lowered = name.lower()
        if lowered == "host":
            hosts.append(value)
        elif lowered in {"content-length", "transfer-encoding"}:
            raise _invalid()
    if hosts != [f"{domain}:443"]:
        raise _invalid()
    return domain


def _read_capability(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        metadata = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o444}
            or metadata.st_dev != current.st_dev
            or metadata.st_ino != current.st_ino
        ):
            raise ValueError("egress adapter configuration is invalid")
        body = os.read(descriptor, 128)
        if os.read(descriptor, 1):
            raise ValueError("egress adapter configuration is invalid")
    except (OSError, ValueError):
        raise ValueError("egress adapter configuration is invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError("egress adapter configuration is invalid") from None
    if _CAPABILITY.fullmatch(capability) is None or body != (capability + "\n").encode():
        raise ValueError("egress adapter configuration is invalid")
    return capability


def load_adapter_config(environment: Mapping[str, str]) -> AdapterConfig:
    try:
        socket_path = Path(environment["AGENT_EGRESS_SOCKET"])
        capability_path = Path(environment["AGENT_EGRESS_CAPABILITY"])
        project_id = validate_project_id(environment["AGENT_PROJECT_ID"])
        agent = validate_agent(environment["AGENT_EGRESS_AGENT"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("egress adapter configuration is invalid") from None
    if not socket_path.is_absolute() or not capability_path.is_absolute():
        raise ValueError("egress adapter configuration is invalid")
    capability = _read_capability(capability_path)
    return AdapterConfig(socket_path, capability, project_id, agent)


def open_gateway_tunnel(
    config: AdapterConfig,
    domain: str,
    sequence: int,
    socket_factory: Callable[..., socket.socket] = socket.socket,
) -> socket.socket:
    gateway = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        gateway.connect(str(config.socket_path))
        request = EgressRequest(
            1,
            config.capability,
            config.project_id,
            sequence,
            "connect",
            domain,
            443,
        )
        gateway.sendall(encode_request_frame(request))
        stream = gateway.makefile("rb")
        try:
            response = read_response_frame(stream)
        finally:
            stream.close()
        if response.status != "ok" or response.code != "connect":
            raise ValueError("egress gateway request failed")
        return gateway
    except (OSError, ValueError):
        gateway.close()
        raise ValueError("egress gateway request failed") from None


def _read_connect_header(client: socket.socket) -> bytes:
    header = bytearray()
    while b"\r\n\r\n" not in header:
        remaining = MAX_CONNECT_HEADER_BYTES - len(header)
        if remaining <= 0:
            raise _invalid()
        chunk = client.recv(min(4096, remaining))
        if not chunk:
            raise _invalid()
        header.extend(chunk)
    return bytes(header)


def handle_connect_client(
    client: socket.socket,
    config: AdapterConfig,
    sequences: EgressSequence,
    *,
    tunnel_opener: Callable[[AdapterConfig, str, int], socket.socket] = open_gateway_tunnel,
) -> RelayCounts | None:
    gateway: socket.socket | None = None
    established = False
    try:
        domain = parse_connect_request(_read_connect_header(client))
        gateway = tunnel_opener(config, domain, sequences.next())
        client.sendall(CONNECTED_RESPONSE)
        established = True
        return relay_tunnel(client, gateway, RelayLimits())
    except (OSError, ValueError):
        if not established:
            try:
                client.sendall(BAD_GATEWAY_RESPONSE)
            except OSError:
                pass
        return None
    finally:
        if gateway is not None:
            gateway.close()


def _serve_client(
    client: socket.socket, config: AdapterConfig, sequences: EgressSequence
) -> None:
    try:
        handle_connect_client(client, config, sequences)
    finally:
        client.close()


def serve_adapter(
    config: AdapterConfig,
    ready_fd: int,
    *,
    listener_factory: Callable[..., socket.socket] = socket.socket,
    selector_factory: Callable[[], selectors.BaseSelector] = selectors.DefaultSelector,
) -> None:
    listeners: list[socket.socket] = []
    sequences = EgressSequence()
    try:
        for family, address in (
            (socket.AF_INET, ("127.0.0.1", 17_843)),
            (socket.AF_INET6, ("::1", 17_843)),
        ):
            listener = listener_factory(family, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(address)
            listener.listen(128)
        try:
            if os.write(ready_fd, b"ready\n") != len(b"ready\n"):
                raise ValueError("egress adapter readiness failed")
        finally:
            os.close(ready_fd)
        with selector_factory() as selector:
            for listener in listeners:
                selector.register(listener, selectors.EVENT_READ)
            while True:
                for key, _events in selector.select():
                    client, _address = key.fileobj.accept()
                    threading.Thread(
                        target=_serve_client,
                        args=(client, config, sequences),
                        daemon=True,
                        name="egress-adapter-client",
                    ).start()
    finally:
        for listener in listeners:
            listener.close()


def run(argv: Sequence[str], environment: Mapping[str, str] = os.environ) -> int:
    if list(argv) == ["--self-check"]:
        return 0
    if len(argv) != 2 or argv[0] != "--ready-fd":
        return 2
    try:
        ready_fd = int(argv[1], 10)
        if ready_fd <= 2:
            raise ValueError
    except ValueError:
        return 2
    try:
        config = load_adapter_config(environment)
        serve_adapter(config, ready_fd)
    except (OSError, ValueError):
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
