from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


def run_hook(environment: dict[str, str], payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.pop("AGENT_HANDOVER_ROOT", None)
    merged.pop("AGENT_PROJECT_ID", None)
    merged.update(environment)
    merged["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agent_container.handover_hook"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env=merged,
    )


class HandoverHookTest(unittest.TestCase):
    def test_missing_environment_is_silent(self) -> None:
        result = run_hook({}, {"hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(result.stdout, "")

    def test_missing_handover_is_silent(self) -> None:
        with TemporaryDirectory() as temp:
            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "agent-container"},
                {"hook_event_name": "SessionStart", "source": "startup"},
            )
            self.assertEqual(result.stdout, "")

    def test_latest_path_is_announced_without_body(self) -> None:
        with TemporaryDirectory() as temp:
            project = Path(temp) / "agent-container"
            project.mkdir()
            handover = project / "2026-08-22_1815.md"
            secret_marker = "body-must-not-be-in-context"
            handover.write_text(secret_marker, encoding="utf-8")

            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "agent-container"},
                {"hook_event_name": "SessionStart", "source": "resume"},
            )
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn(str(handover), context)
            self.assertNotIn(secret_marker, context)
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_invalid_project_id_is_silent_and_does_not_escape_root(self) -> None:
        with TemporaryDirectory() as temp:
            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "../outside"},
                {"hook_event_name": "SessionStart", "source": "startup"},
            )
            self.assertEqual(result.stdout, "")

    def test_hooks_json_registers_only_session_start(self) -> None:
        config = json.loads((ROOT / "profiles" / "codex" / "hooks.json").read_text())
        self.assertEqual(list(config["hooks"]), ["SessionStart"])
        group = config["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "startup|resume|compact")
        handler = group["hooks"][0]
        self.assertIn("agent_container.handover_hook", handler["command"])
        self.assertEqual(handler["additionalContextLimit"], 1000)


if __name__ == "__main__":
    unittest.main()
