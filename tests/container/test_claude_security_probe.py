from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_container.claude_security_probe import ProbeResult
from agent_container.claude_security_probe import main
from agent_container.claude_security_probe import render
from agent_container.claude_security_probe import run_probe


class ClaudeSecurityProbeTest(unittest.TestCase):
    def test_all_denied_renders_exact_boolean_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            proc.mkdir()

            result = run_probe(fixture / "missing-token", proc, {})

            self.assertEqual(result, ProbeResult(False, False, False))
            self.assertEqual(
                render(result),
                "oauth_token_visible=false\n"
                "token_file_readable=false\n"
                "parent_token_via_proc_readable=false\n",
            )

    def test_each_visibility_condition_is_detected_independently(self) -> None:
        sentinel = "DO-NOT-PRINT-CREDENTIAL-BODY"
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            proc.mkdir()
            token = fixture / "token"
            token.write_text(sentinel, encoding="utf-8")
            token.chmod(0o600)

            environment_result = run_probe(
                fixture / "missing", proc, {"CLAUDE_CODE_OAUTH_TOKEN": sentinel}
            )
            self.assertEqual(environment_result, ProbeResult(True, False, False))

            file_result = run_probe(token, proc, {})
            self.assertEqual(file_result, ProbeResult(False, True, False))

            process = proc / "4242"
            process.mkdir()
            (process / "environ").write_bytes(
                f"PATH=/bin\0CLAUDE_CODE_OAUTH_TOKEN={sentinel}\0".encode()
            )
            proc_result = run_probe(fixture / "missing", proc, {})
            self.assertEqual(proc_result, ProbeResult(False, False, True))
            self.assertNotIn(sentinel, render(proc_result))

    def test_unreadable_missing_and_racing_proc_entries_are_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            proc.mkdir()
            (proc / "not-a-pid").mkdir()
            (proc / "100").mkdir()
            (proc / "100/environ").symlink_to(fixture / "vanished")
            (proc / "101").mkdir()
            (proc / "101/environ").mkdir()

            self.assertEqual(
                run_probe(fixture / "missing", proc, {}),
                ProbeResult(False, False, False),
            )

    def test_detects_parent_token_beyond_the_old_64k_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            process = proc / "4242"
            process.mkdir(parents=True)
            (process / "environ").write_bytes(
                b"PADDING="
                + (b"x" * (70 * 1024))
                + b"\0CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT-CREDENTIAL-BODY\0"
            )

            self.assertEqual(
                run_probe(fixture / "missing", proc, {}),
                ProbeResult(False, False, True),
            )

    def test_incomplete_proc_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            process = proc / "4242"
            process.mkdir(parents=True)
            (process / "environ").write_bytes(b"PATH=/bin\0HOME=/workspace\0")

            with patch(
                "agent_container.claude_security_probe._MAX_ENVIRON_BYTES", 8
            ):
                self.assertEqual(
                    run_probe(fixture / "missing", proc, {}),
                    ProbeResult(False, False, True),
                )

            with patch(
                "agent_container.claude_security_probe.os.read",
                side_effect=OSError("DO-NOT-PRINT-CREDENTIAL-BODY"),
            ):
                result = run_probe(fixture / "missing", proc, {})
            self.assertEqual(result, ProbeResult(False, False, True))
            self.assertNotIn("DO-NOT-PRINT", render(result))

    def test_main_prints_only_booleans_and_fails_on_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            proc = fixture / "proc"
            proc.mkdir()
            output = StringIO()

            status = main(
                token_path=fixture / "missing",
                proc_root=proc,
                environment={"CLAUDE_CODE_OAUTH_TOKEN": "DO-NOT-PRINT-CREDENTIAL-BODY"},
                stdout=output,
            )

            self.assertEqual(status, 1)
            self.assertEqual(
                output.getvalue(),
                "oauth_token_visible=true\n"
                "token_file_readable=false\n"
                "parent_token_via_proc_readable=false\n",
            )
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
