import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
import unittest
from unittest.mock import patch

from agent_container.claude_launcher import exec_claude
from agent_container.claude_launcher import load_token
from agent_container.claude_launcher import seed_workspace_trust


class ExecObserved(Exception):
    pass


class ClaudeLauncherTest(unittest.TestCase):
    def write_token(self, directory: Path, value: str = "sk-ant-oat01-" + "t" * 95) -> Path:
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
            token_file = self.write_token(Path(temporary), "sk-ant-oat01-" + "a" * 95)

            self.assertEqual(load_token(token_file), "sk-ant-oat01-" + "a" * 95)

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
            target = self.write_token(directory, "sk-ant-oat01-" + "a" * 95)
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

    def test_load_token_rejects_an_oversized_payload_after_short_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))

            with patch(
                "agent_container.claude_launcher.os.read",
                side_effect=(b"sk-ant-oat01-" + b"a" * 95, b"b" * 4065),
            ) as read_mock:
                with self.assertRaises(ValueError):
                    load_token(token_file)

            self.assertEqual(read_mock.call_count, 2)

    def test_load_token_hides_decode_failure_details_from_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "oauth-token"
            token_file.write_bytes(b"a" * 31 + b"\xff")
            token_file.chmod(0o600)

            with self.assertRaises(ValueError) as caught:
                load_token(token_file)

            formatted = "".join(
                traceback.format_exception(
                    caught.exception,
                    chain=True,
                )
            )
            observed = (
                caught.exception.__cause__ is None,
                caught.exception.__suppress_context__,
                "UnicodeDecodeError" in formatted,
            )
            self.assertEqual(observed, (True, True, False))

    def test_exec_claude_sets_parent_token_without_global_scrub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            observed: dict[str, object] = {}

            def fake_execvpe(program, argv, environment):
                observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
                observed["has_scrub"] = (
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" in environment
                )
                raise ExecObserved

            with patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                    "CLAUDE_CONFIG_DIR": temporary,
                },
            ):
                with self.assertRaises(ExecObserved):
                    exec_claude(token_file, ("claude",), fake_execvpe)

            self.assertEqual(
                observed,
                {"has_token": True, "has_scrub": False},
            )

    def test_exec_claude_forces_demo_mode_over_caller_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            observed: dict[str, str] = {}

            def fake_execvpe(program, argv, environment):
                observed["is_demo"] = environment["IS_DEMO"]
                raise ExecObserved

            with patch.dict(
                os.environ, {"IS_DEMO": "0", "CLAUDE_CONFIG_DIR": temporary}
            ):
                with self.assertRaises(ExecObserved):
                    exec_claude(token_file, ("claude",), fake_execvpe)

            self.assertEqual(observed, {"is_demo": "1"})

    def test_exec_claude_rejects_empty_command_arguments_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))

            self.assert_silent_failure(lambda: exec_claude(token_file, ()))

    def read_config(self, config_dir: Path) -> dict:
        return json.loads((config_dir / ".claude.json").read_text(encoding="utf-8"))

    def test_seed_workspace_trust_creates_a_private_config_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)

            seed_workspace_trust(config_dir, Path("/workspace"))

            config_file = config_dir / ".claude.json"
            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(
                self.read_config(config_dir),
                {"projects": {"/workspace": {"hasTrustDialogAccepted": True}}},
            )

    def test_seed_workspace_trust_marks_an_untrusted_workspace_and_keeps_other_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            config_file.write_text(
                json.dumps(
                    {
                        "numStartups": 13,
                        "projects": {
                            "/workspace": {
                                "allowedTools": [],
                                "hasTrustDialogAccepted": False,
                            },
                            "/other": {"hasTrustDialogAccepted": False},
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_file.chmod(0o600)

            seed_workspace_trust(config_dir, Path("/workspace"))

            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(
                self.read_config(config_dir),
                {
                    "numStartups": 13,
                    "projects": {
                        "/workspace": {
                            "allowedTools": [],
                            "hasTrustDialogAccepted": True,
                        },
                        "/other": {"hasTrustDialogAccepted": False},
                    },
                },
            )

    def test_seed_workspace_trust_adds_a_project_entry_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            config_file.write_text(json.dumps({"numStartups": 1}), encoding="utf-8")
            config_file.chmod(0o600)

            seed_workspace_trust(config_dir, Path("/workspace"))

            self.assertEqual(
                self.read_config(config_dir),
                {
                    "numStartups": 1,
                    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                },
            )

    def test_seed_workspace_trust_leaves_a_trusted_workspace_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            original = b'{"projects": {"/workspace": {"hasTrustDialogAccepted": true}}}'
            config_file.write_bytes(original)
            config_file.chmod(0o600)
            before = config_file.stat()

            seed_workspace_trust(config_dir, Path("/workspace"))

            after = config_file.stat()
            self.assertEqual(config_file.read_bytes(), original)
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_seed_workspace_trust_rejects_invalid_json_without_output_or_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            original = b'{"projects": {"/workspace": SECRET-MARKER'
            config_file.write_bytes(original)
            config_file.chmod(0o600)

            self.assert_silent_failure(
                lambda: seed_workspace_trust(config_dir, Path("/workspace"))
            )
            self.assertEqual(config_file.read_bytes(), original)
            self.assertEqual(sorted(path.name for path in config_dir.iterdir()), [".claude.json"])

    def test_seed_workspace_trust_hides_config_content_from_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            config_file.write_bytes(b'{"oauthAccount": SECRET-MARKER')
            config_file.chmod(0o600)

            with self.assertRaises(ValueError) as caught:
                seed_workspace_trust(config_dir, Path("/workspace"))

            formatted = "".join(traceback.format_exception(caught.exception, chain=True))
            self.assertNotIn("SECRET-MARKER", formatted)
            self.assertNotIn("oauthAccount", formatted)
            self.assertNotIn("JSONDecodeError", formatted)

    def test_seed_workspace_trust_rejects_a_non_object_config_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            config_file.write_bytes(b"[]")
            config_file.chmod(0o600)

            self.assert_silent_failure(
                lambda: seed_workspace_trust(config_dir, Path("/workspace"))
            )
            self.assertEqual(config_file.read_bytes(), b"[]")

    def test_seed_workspace_trust_rejects_a_non_object_project_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            original = b'{"projects": {"/workspace": "unexpected"}}'
            config_file.write_bytes(original)
            config_file.chmod(0o600)

            self.assert_silent_failure(
                lambda: seed_workspace_trust(config_dir, Path("/workspace"))
            )
            self.assertEqual(config_file.read_bytes(), original)

    def test_seed_workspace_trust_rejects_a_symlinked_config_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            target = config_dir / "elsewhere.json"
            target.write_bytes(b"{}")
            target.chmod(0o600)
            (config_dir / ".claude.json").symlink_to(target)

            self.assert_silent_failure(
                lambda: seed_workspace_trust(config_dir, Path("/workspace"))
            )
            self.assertEqual(target.read_bytes(), b"{}")
            self.assertTrue((config_dir / ".claude.json").is_symlink())

    def test_seed_workspace_trust_rejects_a_config_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / ".claude.json"
            config_file.write_bytes(b"{}")
            config_file.chmod(0o600)

            with patch("agent_container.claude_launcher.os.getuid", return_value=os.getuid() + 1):
                self.assert_silent_failure(
                    lambda: seed_workspace_trust(config_dir, Path("/workspace"))
                )
            self.assertEqual(config_file.read_bytes(), b"{}")

    def test_exec_claude_seeds_trust_for_the_working_directory_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token_file = self.write_token(directory)
            config_dir = directory / "claude-config"
            config_dir.mkdir(mode=0o700)
            observed: dict[str, object] = {}

            def fake_execvpe(program, argv, environment):
                observed["config"] = self.read_config(config_dir)
                raise ExecObserved

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
                with patch("agent_container.claude_launcher.os.getcwd", return_value="/workspace"):
                    with self.assertRaises(ExecObserved):
                        exec_claude(token_file, ("claude",), fake_execvpe)

            self.assertEqual(
                observed,
                {"config": {"projects": {"/workspace": {"hasTrustDialogAccepted": True}}}},
            )

    def test_exec_claude_stops_without_exec_when_config_dir_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            calls: list[str] = []

            def fake_execvpe(program, argv, environment):
                calls.append(program)
                raise ExecObserved

            environment = {
                key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assert_silent_failure(
                    lambda: exec_claude(token_file, ("claude",), fake_execvpe)
                )

            self.assertEqual(calls, [])

    def test_exec_claude_stops_without_exec_when_config_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token_file = self.write_token(directory)
            config_file = directory / ".claude.json"
            config_file.write_bytes(b"not json")
            config_file.chmod(0o600)
            calls: list[str] = []

            def fake_execvpe(program, argv, environment):
                calls.append(program)
                raise ExecObserved

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temporary}):
                self.assert_silent_failure(
                    lambda: exec_claude(token_file, ("claude",), fake_execvpe)
                )

            self.assertEqual(calls, [])
            self.assertEqual(config_file.read_bytes(), b"not json")


if __name__ == "__main__":
    unittest.main()
