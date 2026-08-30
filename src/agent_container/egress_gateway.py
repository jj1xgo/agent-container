from dataclasses import dataclass
import ipaddress
import selectors
import socket
import time
from typing import Callable
from typing import Sequence


_CONNECT_TIMEOUT_SECONDS = 15
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class ResolvedTarget:
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple[object, ...]


@dataclass(frozen=True)
class RelayLimits:
    maximum_bytes_per_direction: int = 1 << 31
    idle_timeout_seconds: float = 300
    lifetime_seconds: float = 7_200
    chunk_bytes: int = 65_536


@dataclass(frozen=True)
class RelayCounts:
    from_client: int
    from_upstream: int


def _validated_answer(answer: object) -> ResolvedTarget:
    if not isinstance(answer, tuple) or len(answer) != 5:
        raise ValueError("egress DNS response is invalid")
    family, socktype, protocol, _canonical_name, sockaddr = answer
    if (
        family not in {socket.AF_INET, socket.AF_INET6}
        or socktype != socket.SOCK_STREAM
        or protocol != socket.IPPROTO_TCP
        or not isinstance(sockaddr, tuple)
        or len(sockaddr) != (2 if family == socket.AF_INET else 4)
    ):
        raise ValueError("egress DNS response is invalid")
    host, port = sockaddr[:2]
    if not isinstance(host, str) or isinstance(port, bool) or port != 443:
        raise ValueError("egress DNS response is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("egress DNS response is invalid") from None
    if (family == socket.AF_INET) != (address.version == 4):
        raise ValueError("egress DNS response is invalid")
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address in _CGNAT
    ):
        raise ValueError("egress DNS destination is not allowed")
    canonical = (str(address), *sockaddr[1:])
    if family == socket.AF_INET6:
        flowinfo, scope_id = sockaddr[2:]
        if (
            isinstance(flowinfo, bool)
            or not isinstance(flowinfo, int)
            or isinstance(scope_id, bool)
            or not isinstance(scope_id, int)
            or flowinfo != 0
            or scope_id != 0
        ):
            raise ValueError("egress DNS response is invalid")
    return ResolvedTarget(family, socktype, protocol, canonical)


def resolve_target(
    domain: str,
    resolver: Callable[..., Sequence[tuple[object, ...]]] = socket.getaddrinfo,
) -> tuple[ResolvedTarget, ...]:
    try:
        answers = resolver(
            domain,
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    except OSError:
        raise ValueError("egress DNS resolution failed") from None
    if not isinstance(answers, (list, tuple)) or not answers:
        raise ValueError("egress DNS response is invalid")
    targets: list[ResolvedTarget] = []
    seen: set[ResolvedTarget] = set()
    for answer in answers:
        target = _validated_answer(answer)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    if not targets:
        raise ValueError("egress DNS response is invalid")
    return tuple(targets)


def connect_target(
    target: ResolvedTarget,
    socket_factory: Callable[..., socket.socket] = socket.socket,
) -> socket.socket:
    client = socket_factory(target.family, target.socktype, target.protocol)
    try:
        client.settimeout(_CONNECT_TIMEOUT_SECONDS)
        client.connect(target.sockaddr)
        peer = client.getpeername()
        if (
            not isinstance(peer, tuple)
            or len(peer) < 2
            or peer[0] != target.sockaddr[0]
            or peer[1] != target.sockaddr[1]
        ):
            raise ValueError("egress connected peer does not match resolution")
        return client
    except (OSError, ValueError):
        client.close()
        raise ValueError("egress connection failed") from None


def relay_tunnel(
    client: socket.socket,
    upstream: socket.socket,
    limits: RelayLimits,
    clock: Callable[[], float] = time.monotonic,
    selector_factory: Callable[[], selectors.BaseSelector] = selectors.DefaultSelector,
) -> RelayCounts:
    if (
        isinstance(limits.maximum_bytes_per_direction, bool)
        or not isinstance(limits.maximum_bytes_per_direction, int)
        or limits.maximum_bytes_per_direction < 0
        or limits.idle_timeout_seconds <= 0
        or limits.lifetime_seconds <= 0
        or not 1 <= limits.chunk_bytes <= 65_536
    ):
        raise ValueError("egress relay limits are invalid")
    started = clock()
    last_activity = started
    client_open = True
    upstream_open = True
    client_shutdown = False
    upstream_shutdown = False
    to_client = bytearray()
    to_upstream = bytearray()
    from_client = 0
    from_upstream = 0
    client.setblocking(False)
    upstream.setblocking(False)
    try:
        with selector_factory() as selector:
            selector.register(client, selectors.EVENT_READ, "client")
            selector.register(upstream, selectors.EVENT_READ, "upstream")
            while client_open or upstream_open or to_client or to_upstream:
                client_events = 0
                if client_open and not to_upstream:
                    client_events |= selectors.EVENT_READ
                if to_client:
                    client_events |= selectors.EVENT_WRITE
                upstream_events = 0
                if upstream_open and not to_client:
                    upstream_events |= selectors.EVENT_READ
                if to_upstream:
                    upstream_events |= selectors.EVENT_WRITE
                selector.modify(client, client_events, "client")
                selector.modify(upstream, upstream_events, "upstream")

                now = clock()
                idle_remaining = limits.idle_timeout_seconds - (now - last_activity)
                lifetime_remaining = limits.lifetime_seconds - (now - started)
                timeout = min(idle_remaining, lifetime_remaining)
                if timeout <= 0:
                    raise ValueError("egress relay timed out")
                ready = selector.select(timeout)
                if not ready:
                    raise ValueError("egress relay timed out")
                for key, events in ready:
                    if key.data == "client":
                        if events & selectors.EVENT_READ:
                            body = client.recv(limits.chunk_bytes)
                            if body:
                                from_client += len(body)
                                if from_client > limits.maximum_bytes_per_direction:
                                    raise ValueError("egress relay byte limit exceeded")
                                to_upstream.extend(body)
                                last_activity = clock()
                            else:
                                client_open = False
                        if events & selectors.EVENT_WRITE and to_client:
                            sent = client.send(to_client)
                            if sent <= 0:
                                raise ValueError("egress relay write failed")
                            del to_client[:sent]
                            last_activity = clock()
                    else:
                        if events & selectors.EVENT_READ:
                            body = upstream.recv(limits.chunk_bytes)
                            if body:
                                from_upstream += len(body)
                                if from_upstream > limits.maximum_bytes_per_direction:
                                    raise ValueError("egress relay byte limit exceeded")
                                to_client.extend(body)
                                last_activity = clock()
                            else:
                                upstream_open = False
                        if events & selectors.EVENT_WRITE and to_upstream:
                            sent = upstream.send(to_upstream)
                            if sent <= 0:
                                raise ValueError("egress relay write failed")
                            del to_upstream[:sent]
                            last_activity = clock()

                if not client_open and not to_upstream and not upstream_shutdown:
                    upstream.shutdown(socket.SHUT_WR)
                    upstream_shutdown = True
                if not upstream_open and not to_client and not client_shutdown:
                    client.shutdown(socket.SHUT_WR)
                    client_shutdown = True
    except OSError:
        raise ValueError("egress relay failed") from None
    return RelayCounts(from_client, from_upstream)
