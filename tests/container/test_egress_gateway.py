import socket
import threading
import unittest

from agent_container.egress_gateway import connect_target
from agent_container.egress_gateway import ResolvedTarget
from agent_container.egress_gateway import RelayLimits
from agent_container.egress_gateway import resolve_target
from agent_container.egress_gateway import relay_tunnel


class FakeSocket:
    def __init__(self, peer: tuple[object, ...], *, connect_error: OSError | None = None):
        self.peer = peer
        self.connect_error = connect_error
        self.timeout: float | None = None
        self.connect_calls: list[tuple[object, ...]] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[object, ...]) -> None:
        self.connect_calls.append(address)
        if self.connect_error is not None:
            raise self.connect_error

    def getpeername(self) -> tuple[object, ...]:
        return self.peer

    def close(self) -> None:
        self.closed = True


class EgressGatewayResolutionTest(unittest.TestCase):
    def test_resolves_safe_ipv4_ipv6_and_deduplicates_in_order(self) -> None:
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            ),
        ]
        calls = []

        targets = resolve_target(
            "example.com",
            resolver=lambda *args: calls.append(args) or answers,
        )

        self.assertEqual(
            calls,
            [
                (
                    "example.com",
                    443,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                )
            ],
        )
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].sockaddr, ("93.184.216.34", 443))
        self.assertEqual(targets[1].family, socket.AF_INET6)

    def test_rejects_empty_malformed_mixed_and_non_global_answers(self) -> None:
        unsafe_addresses = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "0.0.0.0",
            "192.0.2.1",
            "100.64.0.1",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
            "2001:db8::1",
        )
        cases = [
            [],
            [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("93.184.216.34", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
        ]
        cases.extend(
            [[(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0) if ":" in address else (address, 443))]]
            for address in unsafe_addresses
        )
        cases.append(
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        )
        for answers in cases:
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                resolve_target("example.com", resolver=lambda *_: answers)

    def test_resolver_errors_are_fixed_and_do_not_echo_domain(self) -> None:
        marker = "private-marker.example.com"
        with self.assertRaises(ValueError) as raised:
            resolve_target(
                marker,
                resolver=lambda *_: (_ for _ in ()).throw(socket.gaierror(marker)),
            )
        self.assertNotIn(marker, str(raised.exception))


class EgressGatewayConnectTest(unittest.TestCase):
    def test_connects_once_with_timeout_and_exact_peer(self) -> None:
        target = ResolvedTarget(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("93.184.216.34", 443),
        )
        created = []
        client = FakeSocket(target.sockaddr)

        connected = connect_target(
            target,
            socket_factory=lambda *args: created.append(args) or client,
        )

        self.assertIs(connected, client)
        self.assertEqual(
            created,
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)],
        )
        self.assertEqual(client.timeout, 15)
        self.assertEqual(client.connect_calls, [target.sockaddr])

    def test_connect_failure_or_peer_mismatch_closes_without_retry(self) -> None:
        target = ResolvedTarget(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("93.184.216.34", 443),
        )
        for client in (
            FakeSocket(target.sockaddr, connect_error=TimeoutError("private")),
            FakeSocket(("93.184.216.35", 443)),
            FakeSocket(("93.184.216.34", 80)),
        ):
            calls = []
            with self.subTest(peer=client.peer), self.assertRaises(ValueError):
                connect_target(
                    target,
                    socket_factory=lambda *args: calls.append(args) or client,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(client.connect_calls), 1)
            self.assertTrue(client.closed)


class EgressGatewayRelayTest(unittest.TestCase):
    def test_relays_both_directions_and_preserves_half_close(self) -> None:
        client_peer, client_gateway = socket.socketpair()
        upstream_gateway, upstream_peer = socket.socketpair()
        result = []
        failure = []
        client_peer.settimeout(1)
        upstream_peer.settimeout(1)

        def run_relay() -> None:
            try:
                result.append(
                    relay_tunnel(
                        client_gateway,
                        upstream_gateway,
                        RelayLimits(),
                    )
                )
            except BaseException as error:
                failure.append(error)

        thread = threading.Thread(target=run_relay)
        try:
            thread.start()
            client_peer.sendall(b"request")
            client_peer.shutdown(socket.SHUT_WR)
            upstream_peer.sendall(b"response")
            upstream_peer.shutdown(socket.SHUT_WR)

            self.assertEqual(upstream_peer.recv(64), b"request")
            self.assertEqual(upstream_peer.recv(64), b"")
            self.assertEqual(client_peer.recv(64), b"response")
            self.assertEqual(client_peer.recv(64), b"")
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failure, [])
            self.assertEqual(result[0].from_client, 7)
            self.assertEqual(result[0].from_upstream, 8)
        finally:
            client_peer.close()
            client_gateway.close()
            upstream_gateway.close()
            upstream_peer.close()

    def test_enforces_per_direction_byte_limit(self) -> None:
        client_peer, client_gateway = socket.socketpair()
        upstream_gateway, upstream_peer = socket.socketpair()
        client_peer.settimeout(1)
        failure = []

        def run_relay() -> None:
            try:
                relay_tunnel(
                    client_gateway,
                    upstream_gateway,
                    RelayLimits(maximum_bytes_per_direction=3),
                )
            except BaseException as error:
                failure.append(error)

        thread = threading.Thread(target=run_relay)
        try:
            thread.start()
            client_peer.sendall(b"four")
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failure), 1)
            self.assertIsInstance(failure[0], ValueError)
            self.assertIn("byte limit", str(failure[0]))
        finally:
            client_peer.close()
            client_gateway.close()
            upstream_gateway.close()
            upstream_peer.close()

    def test_uses_the_earlier_idle_or_lifetime_deadline(self) -> None:
        class FakeSocket:
            def setblocking(self, _: bool) -> None:
                pass

        class NoReadySelector:
            def __init__(self) -> None:
                self.timeouts = []

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                pass

            def register(self, *_: object) -> None:
                pass

            def modify(self, *_: object) -> None:
                pass

            def select(self, timeout: float):
                self.timeouts.append(timeout)
                return []

        for limits, expected_timeout in (
            (RelayLimits(idle_timeout_seconds=300, lifetime_seconds=7_200), 300),
            (RelayLimits(idle_timeout_seconds=10_000, lifetime_seconds=7_200), 7_200),
        ):
            selector = NoReadySelector()
            with self.subTest(expected_timeout=expected_timeout), self.assertRaisesRegex(
                ValueError, "timed out"
            ):
                relay_tunnel(
                    FakeSocket(),  # type: ignore[arg-type]
                    FakeSocket(),  # type: ignore[arg-type]
                    limits,
                    clock=lambda: 0,
                    selector_factory=lambda: selector,  # type: ignore[arg-type]
                )
            self.assertEqual(selector.timeouts, [expected_timeout])


if __name__ == "__main__":
    unittest.main()
