import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "container/bin/agent-handover"


VALID_BODY = """## 作業の目的
目的
## 現在地
現在地
## 決定事項と理由
決定
## 変更したファイル・commit・PR
変更
## 検証結果
検証
## 未解決事項とリスク
リスク
## 次の一手
次
"""


class AgentHandoverWrapperTest(unittest.TestCase):
    def test_create_without_complete_broker_environment_never_writes_directly(
        self,
    ) -> None:
        cases = (
            ("missing-both", {}),
            ("missing-both-with-session", {"CODEX_SESSION_ID": "session-123"}),
            ("socket-only", {"AGENT_HANDOVER_BROKER_SOCKET": "/missing/socket"}),
            (
                "capability-only",
                {"AGENT_HANDOVER_BROKER_CAPABILITY": "/missing/capability"},
            ),
            (
                "connection-failure",
                {
                    "AGENT_HANDOVER_BROKER_SOCKET": "/missing/socket",
                    "AGENT_HANDOVER_BROKER_CAPABILITY": "/missing/capability",
                },
            ),
        )
        for name, broker_environment in cases:
            with self.subTest(case=name), TemporaryDirectory() as temp:
                handover_root = Path(temp) / "handovers"
                project = handover_root / "project"
                project.mkdir(parents=True)
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "AGENT_HANDOVER_BROKER_SOCKET",
                        "AGENT_HANDOVER_BROKER_CAPABILITY",
                        "CODEX_SESSION_ID",
                    }
                }
                environment.update(
                    {
                        "PYTHONPATH": str(ROOT / "src"),
                        "AGENT_HANDOVER_ROOT": str(handover_root),
                        "AGENT_PROJECT_ID": "project",
                        **broker_environment,
                    }
                )

                completed = subprocess.run(
                    (str(WRAPPER), "create", "--title", "Safe title"),
                    env=environment,
                    input=VALID_BODY,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
                self.assertEqual(list(project.iterdir()), [])

    def test_rejects_scope_override_arguments(self) -> None:
        completed = subprocess.run(
            (
                str(WRAPPER),
                "create",
                "--title",
                "Safe title",
                "--root",
                "/tmp/override",
            ),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)

    def test_create_routes_stdin_only_to_broker_client(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            stdin_capture = root / "stdin"
            python = bin_dir / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$AGENT_WRAPPER_CALLS\"\n"
                "sed -n '1,$p' > \"$AGENT_WRAPPER_STDIN\"\n"
                "printf '%s\\n' '/handovers/project/2026-08-27_123456_abcdef12.md'\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            socket_path = root / "broker.sock"
            capability_path = root / "capability"
            environment = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "AGENT_PROJECT_ID": "project",
                "AGENT_HANDOVER_BROKER_SOCKET": str(socket_path),
                "AGENT_HANDOVER_BROKER_CAPABILITY": str(capability_path),
                "AGENT_WRAPPER_CALLS": str(calls),
                "AGENT_WRAPPER_STDIN": str(stdin_capture),
            }

            completed = subprocess.run(
                (str(WRAPPER), "create", "--title", "Safe title"),
                env=environment,
                input=VALID_BODY,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                arguments,
                [
                    "-m",
                    "agent_container.handover_broker_client",
                    "create",
                    "--title=Safe title",
                ],
            )
            self.assertNotIn("handover_cli", calls.read_text(encoding="utf-8"))
            self.assertEqual(stdin_capture.read_text(encoding="utf-8"), VALID_BODY)
            self.assertEqual(
                completed.stdout.strip(),
                "/handovers/project/2026-08-27_123456_abcdef12.md",
            )

    def test_create_rejects_scope_override_without_echoing_stdin(self) -> None:
        environment = {
            **os.environ,
            "AGENT_PROJECT_ID": "project",
            "AGENT_HANDOVER_BROKER_SOCKET": "/run/agent-handover/broker.sock",
            "AGENT_HANDOVER_BROKER_CAPABILITY": "/run/agent-handover/capability",
        }
        marker = "private-rejected-stdin-marker"

        completed = subprocess.run(
            (
                str(WRAPPER),
                "create",
                "--title",
                "Safe title",
                "--project",
                "other",
            ),
            env=environment,
            input=marker,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)
        self.assertNotIn(marker, completed.stdout + completed.stderr)
