"""Opt-in rootless Podman evidence that Codex tool commands keep the container network.

Runs the real `codex exec` inside the runtime container against a loopback mock
Responses API (no credentials, `--network=none`). The mocked model issues one
`exec_command` tool call; the sandboxed command must be able to connect to a
Unix socket the way the family, GitHub and egress clients do.
"""

import json
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

from agent_container.handover_broker_runtime import HandoverBrokerRuntime
from agent_container.podman import run_codex_spec
from agent_container.state import StateLayout


ROOT = Path(__file__).resolve().parents[2]
RUN_PODMAN_INTEGRATION = (
    os.environ.get("AGENT_CONTAINER_RUN_PODMAN_INTEGRATION") == "1"
)
BASE_IMAGE = os.environ.get(
    "AGENT_CONTAINER_INTEGRATION_BASE_IMAGE",
    "localhost/agent-container:dev",
)

MOCK_PROVIDER = textwrap.dedent(
    """
    [model_providers.mock]
    name = "mock"
    base_url = "http://127.0.0.1:8099/v1"
    env_key = "MOCK_API_KEY"
    wire_api = "responses"
    supports_websockets = false
    request_max_retries = 0
    stream_max_retries = 0
    """
)

MOCK_SERVER = textwrap.dedent(
    """
    import json, os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    CMD = os.environ["MOCK_CMD"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            done = any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in body.get("input", []))
            if done:
                items = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": "done", "annotations": []}]}]
            else:
                items = [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "exec_command",
                          "arguments": json.dumps({"cmd": CMD}), "status": "completed"}]
            events = [{"type": "response.created", "response": {"id": "r", "object": "response", "status": "in_progress", "output": []}}]
            for index, item in enumerate(items):
                events.append({"type": "response.output_item.added", "output_index": index, "item": item})
                events.append({"type": "response.output_item.done", "output_index": index, "item": item})
            usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                     "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
            events.append({"type": "response.completed", "response": {"id": "r", "object": "response", "status": "completed", "output": items, "usage": usage}})
            payload = "".join(f"event: {e['type']}\\ndata: {json.dumps(e)}\\n\\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
    """
)

SOCKET_SERVER = textwrap.dedent(
    """
    import os, socket
    path = "/workspace/probe.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(4)
    while True:
        client, _ = server.accept()
        client.close()
    """
)

HANDOVER_BODY = """## 作業の目的
Podman integration fixture
## 現在地
Codex session ID（agent申告・host未検証）: 00000000-0000-4000-8000-000000000123
## 決定事項と理由
create-only broker
## 変更したファイル・commit・PR
fixture only
## 検証結果
fixture request
## 未解決事項とリスク
no credentials
## 次の一手
finish probe
"""

PROBE = textwrap.dedent(
    r'''
    import errno, os, socket, subprocess, sys
    from pathlib import Path
    print("network_disabled_env=" + repr(os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED")))
    with socket.socket(socket.AF_UNIX) as client:
        client.connect("/workspace/probe.sock")
        print("unix_connect=ok")
    project = Path("/handovers/agent-container")
    existing = project / "existing.md"
    assert existing.read_text() == "unchanged fixture\n"
    for mutation in (
        lambda: (project / "direct.md").write_text("direct"),
        lambda: existing.write_text("overwrite"),
        lambda: existing.rename(project / "renamed.md"),
        lambda: existing.unlink(),
    ):
        try:
            mutation()
        except OSError as error:
            if "--mount-only" in sys.argv:
                assert error.errno == errno.EROFS
            else:
                assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
        else:
            raise AssertionError("handover direct mutation succeeded")
    assert existing.read_text() == "unchanged fixture\n"
    print("handover_mutations=denied")
    if "--mount-only" in sys.argv:
        print("outer_handover_mount=read-only")
        raise SystemExit(0)
    body = Path("/workspace/handover-body.md").read_text()
    result = subprocess.run(
        ("agent-handover", "create", "--title", "Podman handover fixture"),
        input=body, text=True, capture_output=True, check=True, timeout=10,
        env={**os.environ, "CODEX_SESSION_ID": "00000000-0000-4000-8000-000000000123"},
    )
    created = Path(result.stdout.strip())
    assert created.parent == project and result.stderr == ""
    document = created.read_text()
    assert "- Session: （未記録）\n" in document
    assert document.endswith(body)
    print("handover_create=ok")
    '''
)

PAYLOAD = """
set -u
export MOCK_API_KEY=sk-offline-dummy MOCK_CMD="python3 /workspace/probe.py"
python3 /workspace/socket_server.py &
python3 /workspace/mock_server.py &
sleep 1
python3 /workspace/probe.py --mount-only || exit $?
cd /workspace
codex --approve-for-me exec --ephemeral --json --color never --skip-git-repo-check "run the tool"
status=$?
kill %1 %2 2>/dev/null
exit $status
"""


@unittest.skipUnless(
    RUN_PODMAN_INTEGRATION,
    "set AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 for real Podman tests",
)
class CodexSandboxNetworkPodmanIntegrationTest(unittest.TestCase):
    # Break caught: the shipped Codex profile letting the tool sandbox disable
    # networking again, so agent-family / broker clients get EPERM on connect.
    def test_exec_tool_command_can_connect_to_unix_socket(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="acn-") as temporary,
            ExitStack() as brokers,
        ):
            layout, handover_project = self._runtime_state(Path(temporary))
            profile = (ROOT / "profiles/codex/config.toml").read_text(encoding="utf-8")
            (layout.codex_home / "config.toml").write_text(
                'model = "gpt-5"\nmodel_provider = "mock"\n' + profile + MOCK_PROVIDER,
                encoding="utf-8",
            )
            (layout.workspace / "mock_server.py").write_text(MOCK_SERVER, encoding="utf-8")
            (layout.workspace / "socket_server.py").write_text(SOCKET_SERVER, encoding="utf-8")
            (layout.workspace / "probe.py").write_text(PROBE, encoding="utf-8")
            body_file = layout.workspace / "handover-body.md"
            body_file.write_text(HANDOVER_BODY, encoding="utf-8")
            body_file.chmod(0o600)
            (handover_project / "existing.md").write_text("unchanged fixture\n")
            handover_broker = brokers.enter_context(
                HandoverBrokerRuntime.create(layout, handover_project)
            )
            spec = run_codex_spec(
                layout, handover_project, BASE_IMAGE, os.getuid(), os.getgid(),
                handover_broker,
            )
            argv = [
                argument
                for argument in spec.argv
                if argument not in {"--interactive", "--tty"}
            ]
            image_index = argv.index(BASE_IMAGE)
            argv.insert(image_index, "--network=none")
            launcher_end = argv.index("--", image_index + 1)
            command = (*argv[: launcher_end + 1], "/bin/sh", "-c", PAYLOAD)

            completed = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=300
            )

            self.assertEqual(
                (handover_project / "existing.md").read_text(), "unchanged fixture\n"
            )
            published = list(handover_project.glob("20*.md"))
            self.assertEqual(len(published), 1, completed.stderr[-800:])
            self.assertTrue(published[0].read_text().endswith(HANDOVER_BODY))
            self.assertEqual(published[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(handover_project.iterdir())), 2)
            brokers.close()
            self.assertFalse(handover_broker.run_dir.exists())

        outputs = []
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                outputs.append(str(item.get("aggregated_output", "")))
        self.assertEqual(
            completed.returncode, 0, f"stderr={completed.stderr[-800:]!r}"
        )
        self.assertIn("outer_handover_mount=read-only", completed.stdout)
        self.assertEqual(len(outputs), 1, completed.stdout[-800:])
        self.assertIn("network_disabled_env=None", outputs[0])
        self.assertIn("unix_connect=ok", outputs[0])
        self.assertIn("handover_mutations=denied", outputs[0])
        self.assertIn("handover_create=ok", outputs[0])

    @staticmethod
    def _runtime_state(root: Path) -> tuple[StateLayout, Path]:
        state = root / "state"
        handovers = root / "handovers"
        for directory in (
            state,
            state / "shared-auth/codex",
            state / "gh",
            state / "projects/agent-container/codex-home",
            state / "projects/agent-container/cache",
            state / "workspaces/agent-container/.git",
            handovers / "agent-container",
        ):
            directory.mkdir(mode=0o700, parents=True)
        auth = state / "shared-auth/codex/auth.json"
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
        layout = StateLayout(state.resolve(), "agent-container")
        return layout, (handovers / "agent-container").resolve()


if __name__ == "__main__":
    unittest.main()
