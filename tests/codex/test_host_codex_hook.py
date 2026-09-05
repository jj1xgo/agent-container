import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles" / "host-codex"
HOOK = PROFILE / "hooks" / "handover-discover"


class HostCodexHandoverHookTest(unittest.TestCase):
    def run_hook(self, home: Path, payload: object, publisher: str | None):
        workspace = home / "workspace"
        workspace.mkdir(exist_ok=True)
        if publisher is not None:
            executable = home / ".local/libexec/agent-container/agent-handover-host"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\n" + publisher, encoding="utf-8")
            executable.chmod(0o755)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), text=True, capture_output=True,
            env={"HOME": str(home), "PATH": os.environ["PATH"]},
            timeout=5, check=False,
        )

    def test_announces_path_and_git_check_for_each_session_start_source(self):
        for source in ("startup", "resume", "compact"):
            with self.subTest(source=source), TemporaryDirectory() as temp:
                home = Path(temp)
                handover = home / "latest.md"
                handover.write_text("body-must-not-be-injected", encoding="utf-8")
                payload = {
                    "hook_event_name": "SessionStart", "source": source,
                    "cwd": str(home / "workspace"),
                }
                result = self.run_hook(home, payload, (
                    'test "$#" = 1 && test "$1" = discover || exit 9\n'
                    'test "$PWD" = "$HOME/workspace" || exit 8\n'
                    'printf "%s\\n" "$HOME/latest.md"\n'
                ))
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "SessionStart")
                self.assertIn(str(handover), output["additionalContext"])
                self.assertIn("必要な場合だけ本文を読み", output["additionalContext"])
                self.assertIn("現在のGit状態と照合", output["additionalContext"])
                self.assertNotIn("body-must-not-be-injected", result.stdout)

    def test_missing_failed_and_timed_out_discovery_are_silent(self):
        for publisher in (None, "exit 0\n", "echo refused >&2\nexit 1\n", "sleep 3\n"):
            with self.subTest(publisher=publisher), TemporaryDirectory() as temp:
                home = Path(temp)
                result = self.run_hook(home, {
                    "hook_event_name": "SessionStart", "cwd": str(home / "workspace"),
                }, publisher)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_invalid_payload_and_other_events_do_not_discover(self):
        for payload in (None, [], {}, {"hook_event_name": "Stop"},
                        {"hook_event_name": "SessionStart", "cwd": 123}):
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                home = Path(temp)
                result = self.run_hook(home, payload, 'touch "$HOME/called"\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((home / "called").exists())

    def test_profile_registers_installed_hook_at_session_start(self):
        self.assertTrue((PROFILE / "hooks.json").is_file())
        config = json.loads((PROFILE / "hooks.json").read_text())
        group = config["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "startup|resume|compact")
        hook = group["hooks"][0]
        self.assertEqual(hook["command"],
                         "/usr/bin/python3 /home/tsu/.local/libexec/agent-container/codex-handover-discover")
        self.assertEqual(hook["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
