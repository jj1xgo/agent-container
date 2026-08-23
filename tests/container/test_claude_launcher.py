import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_container.claude_launcher import exec_claude
from agent_container.claude_launcher import load_token


class ExecObserved(Exception):
    pass


class ClaudeLauncherTest(unittest.TestCase):
    def write_token(self, directory: Path, value: str = "t" * 32) -> Path:
        path = directory / "oauth-token"
        path.write_text(value, encoding="ascii")
        path.chmod(0o600)
        return path

    def assert_silent_failure(self, callback) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.assertRaises((FileNotFoundError, OSError, PermissionError, ValueError)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                callback()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_load_token_reads_a_private_regular_ascii_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary), "a" * 32)

            self.assertEqual(load_token(token_file), "a" * 32)

    def test_load_token_requests_no_follow_when_opening_the_token_path(self) -> None:
        with patch(
            "agent_container.claude_launcher.os.open", side_effect=FileNotFoundError
        ) as open_mock:
            with self.assertRaises(FileNotFoundError):
                load_token(Path("/missing/oauth-token"))

            flags = open_mock.call_args.args[1]
            self.assertEqual(flags & os.O_NOFOLLOW, os.O_NOFOLLOW)

    def test_load_token_rejects_a_missing_file_without_output(self) -> None:
        self.assert_silent_failure(lambda: load_token(Path("/missing/oauth-token")))

    def test_load_token_rejects_a_symlink_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = self.write_token(directory, "a" * 32)
            link = directory / "link"
            link.symlink_to(target)

            self.assert_silent_failure(lambda: load_token(link))

    def test_load_token_rejects_a_non_regular_file_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            self.assert_silent_failure(lambda: load_token(directory))

    def test_load_token_rejects_an_insecure_mode_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            token_file.chmod(0o644)

            self.assert_silent_failure(lambda: load_token(token_file))

    def test_load_token_rejects_a_file_owned_by_another_user_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))

            with patch("agent_container.claude_launcher.os.getuid", return_value=os.getuid() + 1):
                self.assert_silent_failure(lambda: load_token(token_file))

    def test_load_token_rejects_an_invalid_token_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary), "not a valid token")

            self.assert_silent_failure(lambda: load_token(token_file))

    def test_exec_claude_sets_the_token_and_scrubs_subprocess_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            observed: dict[str, object] = {}

            def fake_execvpe(program, argv, environment):
                observed["program"] = program
                observed["argv"] = argv
                observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
                observed["scrub"] = environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"]
                raise ExecObserved

            with self.assertRaises(ExecObserved):
                exec_claude(token_file, ("claude", "auth", "status"), fake_execvpe)

            self.assertEqual(
                observed,
                {
                    "program": "claude",
                    "argv": ("claude", "auth", "status"),
                    "has_token": True,
                    "scrub": "1",
                },
            )

    def test_exec_claude_rejects_empty_command_arguments_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))

            self.assert_silent_failure(lambda: exec_claude(token_file, ()))


if __name__ == "__main__":
    unittest.main()
