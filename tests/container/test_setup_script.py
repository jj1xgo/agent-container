from pathlib import Path
import subprocess
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
