from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HostHandoverBundleTest(unittest.TestCase):
    def test_builds_standalone_executable_outside_the_checkout(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp) / "agent-handover-host"

            subprocess.run(
                (ROOT / "bin/build-agent-handover-host", output),
                check=True,
                cwd=ROOT,
            )
            completed = subprocess.run(
                (output, "--help"),
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
            self.assertIn("{publish,discover}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
