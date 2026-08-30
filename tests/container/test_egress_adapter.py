import io
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from agent_container.egress_adapter import AdapterConfig
from agent_container.egress_adapter import BAD_GATEWAY_RESPONSE
from agent_container.egress_adapter import CONNECTED_RESPONSE
from agent_container.egress_adapter import EgressSequence
from agent_container.egress_adapter import handle_connect_client
from agent_container.egress_adapter import MAX_CONNECT_HEADER_BYTES
from agent_container.egress_adapter import load_adapter_config
from agent_container.egress_adapter import open_gateway_tunnel
from agent_container.egress_adapter import parse_connect_request
from agent_container.egress_adapter import serve_adapter
from agent_container.egress_adapter import run as run_adapter
from agent_container.egress_broker_protocol import decode_request_frame
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import encode_response_frame


class ConnectRequestTest(unittest.TestCase):
    def test_accepts_only_canonical_connect_with_matching_host(self) -> None:
        request = (
            b"CONNECT api.example.com:443 HTTP/1.1\r\n"
            b"Host: api.example.com:443\r\n\r\n"
        )

        self.assertEqual(parse_connect_request(request), "api.example.com")
        self.assertEqual(
            CONNECTED_RESPONSE,
            b"HTTP/1.1 200 Connection Established\r\n\r\n",
        )
        self.assertEqual(BAD_GATEWAY_RESPONSE, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

    def test_rejects_noncanonical_request_targets_and_methods(self) -> None:
        invalid = (
            b"GET api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n",
            b"CONNECT https://api.example.com HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n",
            b"CONNECT 203.0.113.1:443 HTTP/1.1\r\nHost: 203.0.113.1:443\r\n\r\n",
            b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\nHost: [2001:db8::1]:443\r\n\r\n",
            b"CONNECT user@api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n",
            b"CONNECT api.example.com:8443 HTTP/1.1\r\nHost: api.example.com:8443\r\n\r\n",
            b"CONNECT api.example.com HTTP/1.1\r\nHost: api.example.com\r\n\r\n",
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_connect_request(request)

    def test_rejects_missing_duplicate_or_mismatched_host(self) -> None:
        invalid = (
            b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\nHost: api.example.com:443\r\n\r\n",
            b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: other.example.com:443\r\n\r\n",
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_connect_request(request)

    def test_rejects_body_transfer_encoding_and_pipelining(self) -> None:
        prefix = b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n"
        invalid = (
            prefix + b"Content-Length: 1\r\n\r\nx",
            prefix + b"Transfer-Encoding: chunked\r\n\r\n",
            prefix + b"\r\nGET / HTTP/1.1\r\n\r\n",
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_connect_request(request)

    def test_rejects_invalid_encoding_framing_controls_and_size(self) -> None:
        canonical = b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com:443\r\n\r\n"
        invalid = (
            canonical.replace(b"\r\n", b"\n"),
            canonical.replace(b"api.example.com", b"api.\xff.example", 1),
            canonical.replace(b"Host:", b"Host:\x00"),
            canonical[:-2],
            canonical + b"x",
            b"CONNECT api.example.com:443 HTTP/1.1\r\nX: "
            + b"a" * MAX_CONNECT_HEADER_BYTES
            + b"\r\nHost: api.example.com:443\r\n\r\n",
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_connect_request(request)

    def test_errors_are_fixed_and_do_not_echo_input(self) -> None:
        marker = "SECRET-MARKER"
        request = f"CONNECT {marker}:443 HTTP/1.1\r\n\r\n".encode()
        with self.assertRaisesRegex(ValueError, "^egress CONNECT request is invalid$") as raised:
            parse_connect_request(request)
        self.assertNotIn(marker, str(raised.exception))


class _GatewaySocket:
    def __init__(self, response: bytes) -> None:
        self.response = io.BytesIO(response)
        self.sent = bytearray()
        self.connected_to: object = None
        self.closed = False

    def connect(self, address: object) -> None:
        self.connected_to = address

    def sendall(self, body: bytes) -> None:
        self.sent.extend(body)

    def makefile(self, _mode: str) -> io.BytesIO:
        return self.response

    def close(self) -> None:
        self.closed = True


class AdapterGatewayTest(unittest.TestCase):
    def test_cli_self_check_is_mount_free_and_rejects_other_shapes(self) -> None:
        self.assertEqual(run_adapter(["--self-check"], {}), 0)
        self.assertEqual(run_adapter([], {}), 2)
        self.assertEqual(run_adapter(["--ready-fd", "0"], {}), 2)

    def test_loads_exact_fixed_environment_and_read_only_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "broker.sock"
            capability_path = root / "capability"
            capability_path.write_text("A" * 43 + "\n", encoding="ascii")
            capability_path.chmod(0o400)
            environment = {
                "AGENT_EGRESS_SOCKET": str(socket_path),
                "AGENT_EGRESS_CAPABILITY": str(capability_path),
                "AGENT_PROJECT_ID": "demo-project",
                "AGENT_EGRESS_AGENT": "codex",
            }

            config = load_adapter_config(environment)

            self.assertEqual(config.socket_path, socket_path)
            self.assertEqual(config.project_id, "demo-project")
            self.assertEqual(config.agent, "codex")
            self.assertNotIn("A" * 43, repr(config))

    def test_rejects_writable_symlink_or_malformed_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "broker.sock"
            capability_path = root / "capability"
            base = {
                "AGENT_EGRESS_SOCKET": str(socket_path),
                "AGENT_EGRESS_CAPABILITY": str(capability_path),
                "AGENT_PROJECT_ID": "demo-project",
                "AGENT_EGRESS_AGENT": "codex",
            }
            cases = (("writable", 0o600), ("bad value\n", 0o400))
            for body, mode in cases:
                capability_path.write_text(body, encoding="ascii")
                capability_path.chmod(mode)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    load_adapter_config(base)
            target = root / "target"
            target.write_text("A" * 43 + "\n", encoding="ascii")
            target.chmod(0o400)
            capability_path.unlink()
            capability_path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_adapter_config(base)

    def test_allocates_positive_monotonic_sequences(self) -> None:
        sequence = EgressSequence()
        self.assertEqual((sequence.next(), sequence.next()), (1, 2))

    def test_opens_authenticated_tunnel_and_returns_connected_socket(self) -> None:
        gateway = _GatewaySocket(
            encode_response_frame(EgressResponse(1, "ok", "connect"))
        )
        config = AdapterConfig(
            Path("/run/agent-egress/broker.sock"),
            "capability-secret",
            "demo-project",
            "codex",
        )

        connected = open_gateway_tunnel(
            config,
            "api.example.com",
            7,
            socket_factory=lambda *_args: gateway,
        )

        self.assertIs(connected, gateway)
        self.assertEqual(gateway.connected_to, str(config.socket_path))
        request, consumed = decode_request_frame(bytes(gateway.sent))
        self.assertEqual(consumed, len(gateway.sent))
        self.assertEqual(
            (request.project_id, request.sequence, request.domain, request.port),
            ("demo-project", 7, "api.example.com", 443),
        )

    def test_denial_closes_tunnel_and_uses_fixed_error(self) -> None:
        marker = "SECRET-MARKER"
        gateway = _GatewaySocket(
            encode_response_frame(EgressResponse(1, "denied", "policy"))
        )
        config = AdapterConfig(Path("/run/broker.sock"), marker, "demo", "codex")

        with self.assertRaisesRegex(ValueError, "^egress gateway request failed$") as raised:
            open_gateway_tunnel(
                config,
                "api.example.com",
                1,
                socket_factory=lambda *_args: gateway,
            )

        self.assertTrue(gateway.closed)
        self.assertNotIn(marker, str(raised.exception))

    def test_connect_client_relays_opaque_bytes_after_fixed_success(self) -> None:
        proxy_client, adapter_client = socket.socketpair()
        adapter_gateway, upstream = socket.socketpair()
        config = AdapterConfig(Path("/run/broker.sock"), "A" * 43, "demo", "codex")
        result: list[object] = []

        def serve() -> None:
            try:
                result.append(
                    handle_connect_client(
                        adapter_client,
                        config,
                        EgressSequence(),
                        tunnel_opener=lambda *_args: adapter_gateway,
                    )
                )
            except BaseException as error:
                result.append(error)

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            proxy_client.sendall(
                b"CONNECT api.example.com:443 HTTP/1.1\r\n"
                b"Host: api.example.com:443\r\n\r\n"
            )
            self.assertEqual(
                proxy_client.recv(len(CONNECTED_RESPONSE)), CONNECTED_RESPONSE
            )
            proxy_client.sendall(b"opaque-client-bytes")
            self.assertEqual(upstream.recv(64), b"opaque-client-bytes")
            upstream.sendall(b"opaque-upstream-bytes")
            self.assertEqual(proxy_client.recv(64), b"opaque-upstream-bytes")
            proxy_client.shutdown(socket.SHUT_WR)
            upstream.shutdown(socket.SHUT_WR)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(
                (result[0].from_client, result[0].from_upstream),
                (19, 21),
            )
        finally:
            for channel in (proxy_client, adapter_client, adapter_gateway, upstream):
                channel.close()

    def test_connect_client_returns_fixed_502_without_echoing_denial(self) -> None:
        proxy_client, adapter_client = socket.socketpair()
        marker = "SECRET-MARKER"

        def deny(*_args: object) -> socket.socket:
            raise ValueError(marker)

        worker = threading.Thread(
            target=handle_connect_client,
            args=(
                adapter_client,
                AdapterConfig(Path("/run/broker.sock"), "A" * 43, "demo", "codex"),
                EgressSequence(),
            ),
            kwargs={"tunnel_opener": deny},
        )
        worker.start()
        try:
            proxy_client.sendall(
                b"CONNECT api.example.com:443 HTTP/1.1\r\n"
                b"Host: api.example.com:443\r\n\r\n"
            )
            response = proxy_client.recv(1024)
            worker.join(2)
            self.assertEqual(response, BAD_GATEWAY_RESPONSE)
            self.assertNotIn(marker.encode(), response)
        finally:
            proxy_client.close()
            adapter_client.close()

    def test_relay_failure_after_200_only_closes_established_tunnel(self) -> None:
        proxy_client, adapter_client = socket.socketpair()
        adapter_gateway, upstream = socket.socketpair()

        def serve() -> None:
            try:
                handle_connect_client(
                    adapter_client,
                    AdapterConfig(
                        Path("/run/broker.sock"), "A" * 43, "demo", "codex"
                    ),
                    EgressSequence(),
                    tunnel_opener=lambda *_args: adapter_gateway,
                )
            finally:
                adapter_client.close()

        with mock.patch(
            "agent_container.egress_adapter.relay_tunnel",
            side_effect=ValueError("SECRET-MARKER"),
        ):
            worker = threading.Thread(target=serve)
            worker.start()
            try:
                proxy_client.sendall(
                    b"CONNECT api.example.com:443 HTTP/1.1\r\n"
                    b"Host: api.example.com:443\r\n\r\n"
                )
                self.assertEqual(
                    proxy_client.recv(len(CONNECTED_RESPONSE)), CONNECTED_RESPONSE
                )
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(proxy_client.recv(1024), b"")
            finally:
                proxy_client.close()
                adapter_gateway.close()
                upstream.close()

    def test_server_binds_both_fixed_loopbacks_before_signaling_readiness(self) -> None:
        class Listener:
            def __init__(self) -> None:
                self.bound: object = None
                self.backlog: object = None
                self.closed = False

            def setsockopt(self, *_args: object) -> None:
                pass

            def bind(self, address: object) -> None:
                self.bound = address

            def listen(self, backlog: int) -> None:
                self.backlog = backlog

            def accept(self) -> object:
                raise KeyboardInterrupt

            def close(self) -> None:
                self.closed = True

        listeners: dict[int, Listener] = {}

        def listener_factory(family: int, _kind: int) -> Listener:
            listener = Listener()
            listeners[family] = listener
            return listener

        class Selector:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def register(self, *_args: object) -> None:
                pass

            def select(self):
                raise KeyboardInterrupt

        ready_read, ready_write = os.pipe()
        try:
            with self.assertRaises(KeyboardInterrupt):
                serve_adapter(
                    AdapterConfig(
                        Path("/run/broker.sock"), "A" * 43, "demo", "codex"
                    ),
                    ready_write,
                    listener_factory=listener_factory,
                    selector_factory=Selector,
                )
            ready_write = -1
            self.assertEqual(os.read(ready_read, 8), b"ready\n")
            self.assertEqual(listeners[socket.AF_INET].bound, ("127.0.0.1", 17843))
            self.assertEqual(listeners[socket.AF_INET6].bound, ("::1", 17843))
            self.assertTrue(all(listener.backlog == 128 for listener in listeners.values()))
            self.assertTrue(all(listener.closed for listener in listeners.values()))
        finally:
            os.close(ready_read)
            if ready_write >= 0:
                os.close(ready_write)


if __name__ == "__main__":
    unittest.main()
