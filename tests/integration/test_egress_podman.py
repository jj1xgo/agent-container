import json
from io import StringIO
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from unittest import mock

from agent_container.agentctl import main
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_gateway import ResolvedTarget
from agent_container.egress_policy import enable_egress_policy
from agent_container.egress_policy import EgressPolicy
from agent_container.podman import CommandSpec
from agent_container.podman import run_command_supervised
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout


RUN_PODMAN_INTEGRATION = os.environ.get("AGENT_CONTAINER_RUN_PODMAN_INTEGRATION") == "1"
BASE_IMAGE = os.environ.get(
    "AGENT_CONTAINER_INTEGRATION_BASE_IMAGE", "localhost/agent-container:dev"
)

NETWORK_FIXTURE = r'''
import socket, threading, time
def tcp(family, port, response):
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    listener.bind(("0.0.0.0" if family == socket.AF_INET else "::", port))
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(response)
def udp(family):
    server = socket.socket(family, socket.SOCK_DGRAM)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    server.bind(("0.0.0.0" if family == socket.AF_INET else "::", 18444))
    while True:
        body, address = server.recvfrom(4096)
        server.sendto(b"udp:" + body, address)
for family in (socket.AF_INET, socket.AF_INET6):
    threading.Thread(target=tcp, args=(family, 18443, b"tcp-ok"), daemon=True).start()
    threading.Thread(target=tcp, args=(family, 18080, b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"), daemon=True).start()
    threading.Thread(target=udp, args=(family,), daemon=True).start()
print("ready", flush=True)
while True: time.sleep(60)
'''

NETWORK_PROBE = r'''
import os, socket, sys
mode, hostname, ipv4, ipv6 = sys.argv[1:]
for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
    os.environ.pop(name, None)
def tcp(host, family, port, request, expected):
    client = socket.socket(family, socket.SOCK_STREAM); client.settimeout(2)
    try:
        client.connect((host, port)); client.sendall(request)
        if not client.recv(128).startswith(expected): raise RuntimeError("bad TCP response")
    finally: client.close()
def udp(host, family):
    client = socket.socket(family, socket.SOCK_DGRAM); client.settimeout(2)
    try:
        client.sendto(b"probe", (host, 18444))
        if client.recv(64) != b"udp:probe": raise RuntimeError("bad UDP response")
    finally: client.close()
operations = (
    lambda: socket.getaddrinfo(hostname, 18443, socket.AF_UNSPEC, socket.SOCK_STREAM),
    lambda: tcp(ipv4, socket.AF_INET, 18443, b"probe", b"tcp-ok"),
    lambda: tcp(ipv6, socket.AF_INET6, 18443, b"probe", b"tcp-ok"),
    lambda: tcp(ipv4, socket.AF_INET, 18080, b"GET / HTTP/1.1\r\nHost: fixture\r\n\r\n", b"HTTP/1.1 204"),
    lambda: tcp(ipv6, socket.AF_INET6, 18080, b"GET / HTTP/1.1\r\nHost: fixture\r\n\r\n", b"HTTP/1.1 204"),
    lambda: udp(ipv4, socket.AF_INET), lambda: udp(ipv6, socket.AF_INET6),
)
for operation in operations:
    if mode == "allow": operation(); continue
    try: operation()
    except (OSError, RuntimeError): continue
    raise SystemExit("restricted local-network bypass succeeded")
'''

TLS_PROBE = r'''
import socket, ssl
proxy = socket.create_connection(("127.0.0.1", 17843), timeout=2)
proxy.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n")
response = b""
while b"\r\n\r\n" not in response: response += proxy.recv(4096)
if response != b"HTTP/1.1 200 Connection Established\r\n\r\n": raise SystemExit("approved CONNECT failed")
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
with context.wrap_socket(proxy, server_hostname="allowed.example") as tls:
    tls.sendall(b"podman-private-application-marker")
    if tls.recv(4096) != b"tls-ok": raise SystemExit("TLS response mismatch")
denied = socket.create_connection(("127.0.0.1", 17843), timeout=2)
denied.sendall(b"CONNECT denied.example:443 HTTP/1.1\r\nHost: denied.example:443\r\n\r\n")
response = b""
while b"\r\n\r\n" not in response: response += denied.recv(4096)
denied.close()
if response != b"HTTP/1.1 502 Bad Gateway\r\n\r\n": raise SystemExit("denial failed")
'''

CODEX_PROBE = (
    "#!/usr/bin/python3\n"
    "import os, signal, time\n"
    "mode = os.environ['EGRESS_TEST_MODE']\n"
    "if mode == 'death':\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    while True:\n"
    "        time.sleep(60)\n"
    "elif mode == 'tls':\n"
    + textwrap.indent(TLS_PROBE, "    ")
    + "else:\n"
    "    raise SystemExit('unknown egress test mode')\n"
)


@unittest.skipUnless(
    RUN_PODMAN_INTEGRATION,
    "set AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 for real Podman tests",
)
class EgressPodmanIntegrationTest(unittest.TestCase):
    def test_agentctl_missing_adapter_preflight_launches_no_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ep-") as temporary:
            root = Path(temporary)
            image = f"localhost/agent-container:missing-egress-{uuid.uuid4().hex[:12]}"
            context = root / "image"
            context.mkdir()
            (context / "Containerfile").write_text(
                f"FROM {BASE_IMAGE}\n"
                "USER root\n"
                "RUN rm /usr/local/bin/agent-egress-adapter\n"
                "USER agent\n",
                encoding="utf-8",
            )
            subprocess.run(
                (
                    "podman",
                    "build",
                    "--tag",
                    image,
                    "--file",
                    str(context / "Containerfile"),
                    str(context),
                ),
                check=True,
                capture_output=True,
            )
            self.addCleanup(
                subprocess.run,
                ("podman", "image", "rm", "--force", image),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            state, layout = self._runtime_state(root)
            enable_egress_policy(layout.egress_policy_file)
            containers_before = self._container_names()

            result = main(
                ["--image", image, "run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(state)},
                git_remote_reader=lambda _path: (
                    "https://github.com/jj1xgo/agent-container.git"
                ),
                stdout=StringIO(),
                stderr=StringIO(),
            )

            self.assertEqual(result, 1)
            self.assertEqual(self._container_names(), containers_before)
            self.assertFalse(layout.egress_broker_run_root.exists())

    def test_gateway_death_stops_and_removes_real_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ep-") as temporary:
            root = Path(temporary)
            image = self._build_probe_image(root)
            state = root / "state"
            state.mkdir(mode=0o700)
            runtime = EgressBrokerRuntime.create(
                StateLayout(state.resolve(), "integration-egress"),
                "codex",
                EgressPolicy(1, "allowlist", ("allowed.example",)),
            )
            mount = runtime.__enter__()
            name = mount.container_name
            self.addCleanup(self._remove_container, name)
            failures: list[BaseException] = []

            def fail_gateway_after_start() -> None:
                try:
                    for _ in range(100):
                        if self._container_exists(name):
                            assert runtime._listener is not None
                            runtime._listener.close()
                            return
                        time.sleep(0.1)
                    raise AssertionError("egress runtime did not start")
                except BaseException as error:
                    failures.append(error)

            breaker = threading.Thread(target=fail_gateway_after_start, daemon=True)
            breaker.start()
            command = self._runtime_command(name, mount.run_dir, image, "death")
            try:
                with self.assertRaises(EgressBrokerRuntimeError):
                    run_command_supervised(CommandSpec(command, {}), runtime, mount)
            finally:
                breaker.join(timeout=15)
                with self.assertRaises(EgressBrokerRuntimeError):
                    runtime.__exit__()

            self.assertEqual(failures, [])
            self.assertFalse(breaker.is_alive())
            self.assertFalse(self._container_exists(name))

    def test_network_none_blocks_reachable_local_network_bypasses(self) -> None:
        suffix = uuid.uuid4().hex[:12]
        network = f"egress-integration-{suffix}"
        fixture = f"egress-fixture-{suffix}"
        subprocess.run(("podman", "network", "create", "--ipv6", network), check=True, capture_output=True)
        self.addCleanup(self._cleanup_network, fixture, network)
        subprocess.run(
            ("podman", "run", "--detach", f"--name={fixture}", f"--network={network}",
             "--network-alias=fixture.local", BASE_IMAGE, "python3", "-c", NETWORK_FIXTURE),
            check=True, capture_output=True,
        )
        self._wait_for_log(fixture, "ready")
        networks = json.loads(subprocess.run(
            ("podman", "inspect", "--format", "{{json .NetworkSettings.Networks}}", fixture),
            check=True, capture_output=True, text=True,
        ).stdout)
        attachment = networks[network]
        ipv4, ipv6 = attachment["IPAddress"], attachment["GlobalIPv6Address"]
        self.assertTrue(ipv4)
        self.assertTrue(ipv6)
        baseline = self._run_probe((f"--network={network}",), "allow", ipv4, ipv6)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        restricted = self._run_probe(("--network=none",), "deny", ipv4, ipv6)
        self.assertEqual(restricted.returncode, 0, restricted.stderr)

    def test_network_none_relays_approved_real_tls_and_denies_other_domain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ep-") as temporary:
            root = Path(temporary)
            image = self._build_probe_image(root)
            certificate, private_key = self._create_certificate(root)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private_key)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            address = listener.getsockname()
            def serve() -> None:
                connection, _ = listener.accept()
                with connection, context.wrap_socket(connection, server_side=True) as tls:
                    self.assertEqual(tls.recv(4096), b"podman-private-application-marker")
                    tls.sendall(b"tls-ok")
            fixture = threading.Thread(target=serve, daemon=True)
            fixture.start()
            state = root / "state"
            state.mkdir(mode=0o700)
            runtime = EgressBrokerRuntime.create(
                StateLayout(state.resolve(), "integration-egress"), "codex",
                EgressPolicy(1, "allowlist", ("allowed.example",)),
            )
            target = ResolvedTarget(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("93.184.216.34", 443))
            with mock.patch("agent_container.egress_broker_runtime.resolve_target", return_value=(target,)), \
                 mock.patch("agent_container.egress_broker_runtime.connect_target", side_effect=lambda _target: socket.create_connection(address, timeout=2)), \
                 runtime as mount:
                name = mount.container_name
                self.addCleanup(self._remove_container, name)
                completed = subprocess.run(
                    self._runtime_command(name, mount.run_dir, image, "tls"),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            listener.close()
            fixture.join(timeout=2)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(fixture.is_alive())
            output = completed.stdout + completed.stderr
            for private in ("allowed.example", "denied.example", "podman-private-application-marker"):
                self.assertNotIn(private, output)

    def _run_probe(self, network: tuple[str, ...], mode: str, ipv4: str, ipv6: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("podman", "run", "--rm", *network, BASE_IMAGE, "python3", "-c", NETWORK_PROBE,
             mode, "fixture.local", ipv4, ipv6),
            check=False, capture_output=True, text=True, timeout=30,
        )

    @staticmethod
    def _runtime_command(
        name: str, run_dir: Path, image: str, mode: str
    ) -> tuple[str, ...]:
        return (
            "podman", "run", "--rm", f"--name={name}",
            *(() if mode == "tls" else ("--sig-proxy=false",)),
            "--network=none", "--read-only",
            "--cap-drop=all", "--security-opt=no-new-privileges", "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=64m", "--mount", f"src={run_dir},dst=/run/agent-egress,ro=true",
            "--env", "AGENT_EGRESS_SOCKET=/run/agent-egress/broker.sock", "--env",
            "AGENT_EGRESS_CAPABILITY=/run/agent-egress/capability", "--env", "AGENT_PROJECT_ID=integration-egress",
            "--env", "AGENT_EGRESS_AGENT=codex", "--env", f"EGRESS_TEST_MODE={mode}",
            image, "agent-egress-runtime", "--", "codex",
        )

    @staticmethod
    def _container_exists(name: str) -> bool:
        return subprocess.run(
            ("podman", "container", "exists", name),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    @staticmethod
    def _container_names() -> tuple[str, ...]:
        completed = subprocess.run(
            ("podman", "ps", "--all", "--format", "{{.Names}}"),
            check=True,
            capture_output=True,
            text=True,
        )
        return tuple(sorted(completed.stdout.splitlines()))

    @staticmethod
    def _runtime_state(root: Path) -> tuple[Path, StateLayout]:
        state = root / "state"
        handovers = root / "handovers"
        for directory in (
            state,
            state / "shared-auth",
            state / "shared-auth/codex",
            state / "shared-auth/claude",
            state / "gh",
            state / "projects",
            state / "projects/agent-container",
            state / "projects/agent-container/codex-home",
            state / "projects/agent-container/cache",
            state / "workspaces",
            handovers,
            handovers / "agent-container",
        ):
            directory.mkdir(mode=0o700)
        workspace = state / "workspaces/agent-container"
        (workspace / ".git").mkdir(parents=True)
        auth = state / "shared-auth/codex/auth.json"
        auth.write_text("private-test-placeholder", encoding="utf-8")
        auth.chmod(0o600)
        hosts = state / "gh/hosts.yml"
        hosts.write_text("github.com:\n", encoding="utf-8")
        hosts.chmod(0o600)
        ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), handovers.resolve()
        ).write(state / "projects/agent-container/project.json")
        return state, StateLayout(state.resolve(), "agent-container")

    def _build_probe_image(self, root: Path) -> str:
        context = root / "probe-image"
        context.mkdir()
        (context / "codex").write_text(CODEX_PROBE, encoding="utf-8")
        (context / "Containerfile").write_text(
            f"FROM {BASE_IMAGE}\n"
            "USER root\n"
            "COPY --chmod=0755 codex /usr/local/bin/codex\n"
            "USER agent\n",
            encoding="utf-8",
        )
        image = f"localhost/agent-container:egress-probe-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            (
                "podman",
                "build",
                "--tag",
                image,
                "--file",
                str(context / "Containerfile"),
                str(context),
            ),
            check=True,
            capture_output=True,
        )
        self.addCleanup(
            subprocess.run,
            ("podman", "image", "rm", "--force", image),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return image

    @staticmethod
    def _create_certificate(root: Path) -> tuple[Path, Path]:
        certificate, key = root / "certificate.pem", root / "private-key.pem"
        subprocess.run(
            ("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=allowed.example",
             "-keyout", str(key), "-out", str(certificate), "-days", "1"),
            check=True, capture_output=True,
        )
        return certificate, key

    @staticmethod
    def _wait_for_log(container: str, marker: str) -> None:
        for _ in range(100):
            completed = subprocess.run(("podman", "logs", container), check=False, capture_output=True, text=True)
            if marker in completed.stdout:
                return
            time.sleep(0.1)
        raise AssertionError("Podman network fixture did not become ready")

    @staticmethod
    def _remove_container(name: str) -> None:
        subprocess.run(("podman", "rm", "--force", "--ignore", name), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def _cleanup_network(cls, fixture: str, network: str) -> None:
        cls._remove_container(fixture)
        subprocess.run(("podman", "network", "rm", "--force", network), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
