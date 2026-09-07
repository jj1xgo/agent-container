import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "bin/setup.sh"


class SetupScriptTest(unittest.TestCase):
    def test_shell_syntax_is_valid(self) -> None:
        completed = subprocess.run(("sh", "-n", str(SETUP)), check=False)
        self.assertEqual(completed.returncode, 0)

    def test_rejects_repository_without_owner_before_side_effects(self) -> None:
        completed = subprocess.run(
            ("sh", str(SETUP), "repository"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)

    def test_rejects_unsafe_project_before_side_effects(self) -> None:
        completed = subprocess.run(
            ("sh", str(SETUP), "owner/repository", "../unsafe"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)

    def test_rejects_overlong_project_before_side_effects(self) -> None:
        completed = subprocess.run(
            ("sh", str(SETUP), "owner/repository", "p" * 101),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)

    def test_default_handover_root_is_a_sibling_of_custom_state_root(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            script_dir = root / "bin"
            fake_bin = root / "fake-bin"
            script_dir.mkdir()
            fake_bin.mkdir()
            setup = script_dir / "setup.sh"
            shutil.copy2(SETUP, setup)
            calls = root / "agentctl-calls"
            agentctl = script_dir / "agentctl"
            agentctl.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SETUP_CALLS\"\n",
                encoding="utf-8",
            )
            agentctl.chmod(0o755)
            for name in ("git", "gh", "podman", "python3"):
                command = fake_bin / name
                command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                command.chmod(0o755)
            state_root = root / "state"
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENT_CONTAINER_HOME": str(state_root) + "/",
                "HOME": str(root / "home"),
                "SETUP_CALLS": str(calls),
            }
            environment.pop("AGENT_HANDOVER_ROOT", None)

            completed = subprocess.run(
                ("sh", str(setup), "owner/repository"),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            handover_root = root / "state-handovers"
            self.assertTrue((handover_root / "repository").is_dir())
            self.assertIn(
                f"project add owner/repository --handover-root {handover_root}",
                calls.read_text(encoding="utf-8").splitlines(),
            )
            self.assertFalse((state_root / "handovers").exists())
