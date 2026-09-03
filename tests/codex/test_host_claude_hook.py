import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "profiles" / "host-claude" / "hooks" / "handover-discover"
FOUND = "/handovers/agent-container/2026-09-03_000000_ab.md"


class HostClaudeHandoverHookTest(unittest.TestCase):
    def _run(
        self,
        home: Path,
        payload: dict,
        publisher: str | None,
        env_file: Path | None = None,
    ) -> subprocess.CompletedProcess:
        if publisher is not None:
            executable = home / ".local/libexec/agent-container/agent-handover-host"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(publisher, encoding="utf-8")
            executable.chmod(0o755)
        environment = {"PATH": os.environ["PATH"], "HOME": str(home)}
        if env_file is not None:
            environment["CLAUDE_ENV_FILE"] = str(env_file)
        return subprocess.run(
            [str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )

    def _payload(self, home: Path, **overrides) -> dict:
        payload = {
            "session_id": "0199a1b2-c3d4-7e5f-8a6b-0c1d2e3f4a5b",
            "cwd": str(home / "workspace"),
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        payload.update(overrides)
        return payload

    def test_reports_only_the_path_and_persists_the_session_id(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "workspace").mkdir()
            env_file = home / "env"
            stub = (
                "#!/bin/sh\n"
                'test "$1" = discover || exit 9\n'
                'printf "%s\\n" "$PWD" > "$HOME/called-in"\n'
                f'printf "%s\\n" "{FOUND}"\n'
            )

            completed = self._run(home, self._payload(home), stub, env_file)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn(FOUND, context)
            self.assertIn("必要な場合だけ本文を読み", context)
            self.assertEqual(
                (home / "called-in").read_text(encoding="utf-8").strip(),
                str((home / "workspace").resolve()),
            )
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                "export CLAUDE_SESSION_ID='0199a1b2-c3d4-7e5f-8a6b-0c1d2e3f4a5b'\n",
            )

    def test_stays_silent_without_handover(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "workspace").mkdir()

            completed = self._run(home, self._payload(home), "#!/bin/sh\nexit 0\n")

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")

    def test_stays_silent_when_discovery_is_refused(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "workspace").mkdir()
            stub = "#!/bin/sh\necho refused >&2\nexit 1\n"

            completed = self._run(home, self._payload(home), stub)

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")

    def test_stays_silent_without_installed_publisher(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "workspace").mkdir()

            completed = self._run(home, self._payload(home), None)

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")

    def test_ignores_other_events_and_unsafe_session_ids(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "workspace").mkdir()
            env_file = home / "env"
            stub = f'#!/bin/sh\nprintf "%s\\n" "{FOUND}"\n'

            other = self._run(
                home, self._payload(home, hook_event_name="Stop"), stub, env_file
            )
            unsafe = self._run(
                home,
                self._payload(home, session_id="x'; rm -rf ~ #"),
                stub,
                env_file,
            )

            self.assertEqual(other.returncode, 0)
            self.assertEqual(other.stdout, "")
            self.assertEqual(unsafe.returncode, 0)
            self.assertIn(FOUND, unsafe.stdout)
            self.assertFalse(env_file.exists())


if __name__ == "__main__":
    unittest.main()
