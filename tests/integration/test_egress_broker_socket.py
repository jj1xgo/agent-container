import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from agent_container.egress_adapter import AdapterConfig
from agent_container.egress_adapter import EgressSequence
from agent_container.egress_adapter import handle_connect_client
from agent_container.egress_adapter import open_gateway_tunnel
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_broker_protocol import read_response_frame
from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_gateway import ResolvedTarget
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


RUN_SOCKET_INTEGRATION = (
    os.environ.get("AGENT_CONTAINER_RUN_SOCKET_INTEGRATION") == "1"
)


@unittest.skipUnless(
    RUN_SOCKET_INTEGRATION,
    "set AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 for Unix socket integration",
)
class EgressBrokerSocketIntegrationTest(unittest.TestCase):
    def test_real_adapter_gateway_socket_enforces_policy_and_cleans_runtime(
        self,
    ) -> None:
        payload = b"private-tls-application-marker"
        with tempfile.TemporaryDirectory(prefix="eb-") as temporary:
            temporary_root = Path(temporary)
            certificate = temporary_root / "certificate.pem"
            private_key = temporary_root / "private-key.pem"
            subprocess.run(
                (
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-subj",
                    "/CN=allowed.example",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                ),
                check=True,
                capture_output=True,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate, private_key)
            root = temporary_root / "state"
            root.mkdir(mode=0o700)
            layout = StateLayout(root.resolve(), "agent-container")
            policy = EgressPolicy(1, "allowlist", ("allowed.example",))
            runtime = EgressBrokerRuntime.create(layout, "codex", policy)
            run_dir = runtime.session.run_dir
            capability = runtime.session._capability

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            fixture_address = listener.getsockname()

            def serve_fixture() -> None:
                connection, _ = listener.accept()
                with connection, server_context.wrap_socket(
                    connection, server_side=True
                ) as tls:
                    body = tls.recv(4096)
                    tls.sendall(b"fixture-response:" + body)

            fixture = threading.Thread(target=serve_fixture, daemon=True)
            fixture.start()

            target = ResolvedTarget(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                ("93.184.216.34", 443),
            )

            def connect_fixture(_target: ResolvedTarget) -> socket.socket:
                return socket.create_connection(fixture_address, timeout=2)

            with mock.patch(
                "agent_container.egress_broker_runtime.resolve_target",
                return_value=(target,),
            ), mock.patch(
                "agent_container.egress_broker_runtime.connect_target",
                side_effect=connect_fixture,
            ), runtime as mount:
                for request, expected_code in (
                    (
                        EgressRequest(
                            1,
                            "x" * 43,
                            "agent-container",
                            1,
                            "connect",
                            "allowed.example",
                            443,
                        ),
                        "authentication",
                    ),
                    (
                        EgressRequest(
                            1,
                            capability,
                            "other-project",
                            1,
                            "connect",
                            "allowed.example",
                            443,
                        ),
                        "authentication",
                    ),
                    (
                        EgressRequest(
                            1,
                            capability,
                            "agent-container",
                            1,
                            "connect",
                            "denied.example",
                            443,
                        ),
                        "policy",
                    ),
                ):
                    self.assertEqual(
                        self._request(mount.socket_path, encode_request_frame(request)),
                        ("denied", expected_code),
                    )

                port_frame = self._raw_request_frame(
                    capability=capability,
                    project_id="agent-container",
                    sequence=1,
                    domain="allowed.example",
                    port=80,
                )
                self.assertEqual(
                    self._request(mount.socket_path, port_frame),
                    ("denied", "authentication"),
                )

                adapter = AdapterConfig(
                    mount.socket_path,
                    capability,
                    "agent-container",
                    "codex",
                )
                adapter_peer, proxy_client = socket.socketpair()

                def run_adapter() -> None:
                    with adapter_peer:
                        handle_connect_client(
                            adapter_peer, adapter, EgressSequence()
                        )

                worker = threading.Thread(
                    target=run_adapter,
                    daemon=True,
                )
                worker.start()
                with proxy_client:
                    proxy_client.sendall(
                        b"CONNECT allowed.example:443 HTTP/1.1\r\n"
                        b"Host: allowed.example:443\r\n\r\n"
                    )
                    response = self._receive_until(proxy_client, b"\r\n\r\n")
                    self.assertEqual(
                        response,
                        b"HTTP/1.1 200 Connection Established\r\n\r\n",
                    )
                    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    client_context.check_hostname = False
                    client_context.verify_mode = ssl.CERT_NONE
                    with client_context.wrap_socket(
                        proxy_client, server_hostname="allowed.example"
                    ) as tls:
                        tls.sendall(payload)
                        self.assertEqual(
                            tls.recv(4096), b"fixture-response:" + payload
                        )
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())

                replay = EgressRequest(
                    1,
                    capability,
                    "agent-container",
                    1,
                    "connect",
                    "allowed.example",
                    443,
                )
                self.assertEqual(
                    self._request(mount.socket_path, encode_request_frame(replay)),
                    ("denied", "authentication"),
                )

            listener.close()
            fixture.join(timeout=2)
            self.assertFalse(fixture.is_alive())
            self.assertFalse(run_dir.exists())
            with self.assertRaises(ValueError):
                open_gateway_tunnel(
                    AdapterConfig(
                        run_dir / "broker.sock",
                        capability,
                        "agent-container",
                        "codex",
                    ),
                    "allowed.example",
                    2,
                )

            audit = runtime.session.audit_file.read_text(encoding="utf-8")
            records = [json.loads(line) for line in audit.splitlines()]
            self.assertEqual(
                [(record["status"], record.get("stage")) for record in records],
                [
                    ("denied", "authentication"),
                    ("denied", "authentication"),
                    ("denied", "policy"),
                    ("denied", "authentication"),
                    ("ok", None),
                    ("denied", "authentication"),
                ],
            )
            for secret in (
                capability,
                "allowed.example",
                "denied.example",
                "93.184.216.34",
                payload.decode("latin1"),
            ):
                self.assertNotIn(secret, audit)

    @staticmethod
    def _request(socket_path: Path, frame: bytes) -> tuple[str, str]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(frame)
            stream = client.makefile("rb")
            try:
                response = read_response_frame(stream)
            finally:
                stream.close()
        return response.status, response.code

    @staticmethod
    def _raw_request_frame(**values: object) -> bytes:
        body = json.dumps(
            {"version": 1, "operation": "connect", **values},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return struct.pack(">I", len(body)) + body

    @staticmethod
    def _receive_until(client: socket.socket, marker: bytes) -> bytes:
        body = bytearray()
        while marker not in body:
            body.extend(client.recv(4096))
        return bytes(body)

if __name__ == "__main__":
    unittest.main()
