"""Opt-in rootless Podman evidence for the family intake runtime boundary."""

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from tempfile import TemporaryDirectory
import unittest

from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_intake_runtime import FamilyIntakeRuntimeError
from agent_container.family_pending import list_pending
from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import write_family_binding
from agent_container.podman import CommandSpec
from agent_container.podman import run_claude_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_command_supervised
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.handover_broker_runtime import HandoverRuntimeMount


_IMAGE = os.environ.get("AGENT_FAMILY_TEST_IMAGE", "")
_PROBE_SCRIPT = (
    "set -eu; "
    "test \"$(env | grep -c '^AGENT_FAMILY_')\" -eq 2; "
    "! env | grep -E 'family/roadmap|repository_id|private-key|token'; "
    "! grep -E 'family/roadmap|private-key|/pending' /proc/self/mountinfo; "
    "! find / -xdev -name 'private-key.pem' -print 2>/dev/null | grep .; "
    "agent-family issue create --title T --summary S "
    "--context C --acceptance-criterion A; "
    "sleep 2; "
    "if agent-family issue create --title T --summary S "
    "--context C --acceptance-criterion A >/tmp/second 2>&1; "
    "then exit 91; fi"
)


def _podman_prerequisite() -> str | None:
    if shutil.which("podman") is None:
        return "rootless Podman executable is unavailable"
    probe = subprocess.run(
        ("podman", "info", "--format", "{{.Host.Security.Rootless}}"),
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return "a working rootless Podman service is unavailable"
    if not _IMAGE:
        return "AGENT_FAMILY_TEST_IMAGE is not configured"
    image = subprocess.run(
        ("podman", "image", "exists", _IMAGE), check=False
    )
    if image.returncode != 0:
        return "the instrumented family intake image is unavailable"
    return None


_MISSING = _podman_prerequisite()


class FamilyIntakePodmanFixtureTest(unittest.TestCase):
    def test_instrumented_script_is_strict_and_shell_syntax_is_valid(self) -> None:
        checked = subprocess.run(
            ("/bin/sh", "-n", "-c", _PROBE_SCRIPT),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual((checked.returncode, checked.stderr), (0, ""))
        self.assertTrue(_PROBE_SCRIPT.startswith("set -eu;"))
        self.assertIn("if agent-family issue create", _PROBE_SCRIPT)
        self.assertNotIn("; test $?", _PROBE_SCRIPT)


@unittest.skipIf(_MISSING is not None, _MISSING or "Podman prerequisite missing")
class FamilyIntakePodmanTest(unittest.TestCase):
    def test_real_runtime_ancestry_allows_one_request_and_denies_second(self) -> None:
        for agent in ("codex", "claude"):
            with (
                self.subTest(agent=agent),
                TemporaryDirectory() as temp,
                TemporaryDirectory() as handover_temp,
            ):
                root = Path(temp)
                root.chmod(0o700)
                layout = FamilyStateLayout(root, "demo")
                for directory in (
                    layout.family_root,
                    layout.family_root / "projects",
                    layout.family_project_dir,
                ):
                    directory.mkdir(mode=0o700)
                write_family_binding(
                    layout.family_binding_file,
                    FamilyBinding(Repository.parse("family/roadmap"), 42),
                )
                runtime = FamilyIntakeRuntime.create(layout)
                mount = runtime.start()
                try:
                    payload = _PROBE_SCRIPT
                    state = StateLayout(root, "demo")
                    handover = Path(handover_temp) / "demo"
                    for directory in (
                        state.workspace, state.codex_home, state.cache,
                        state.gh_dir, state.claude_config, handover,
                        Path(handover_temp) / "broker",
                    ):
                        directory.mkdir(parents=True, exist_ok=True)
                    for file_path in (state.codex_auth_file, state.claude_token_file):
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        file_path.write_text("x" * 32, encoding="ascii")
                    base = (
                        run_codex_spec(
                            state, handover, _IMAGE, os.getuid(), os.getgid(),
                            family_mount=mount,
                        )
                        if agent == "codex"
                        else run_claude_spec(
                            state, handover, _IMAGE, os.getuid(), os.getgid(),
                            HandoverRuntimeMount(Path(handover_temp) / "broker"),
                            family_mount=mount,
                        )
                    )
                    image_index = base.argv.index(_IMAGE)
                    argv = base.argv[: image_index + 1] + ("/bin/sh", "-c", payload)

                    inspection = []

                    def inspect_running_container():
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            pending_now = list_pending(
                                layout.family_pending_dir, "demo"
                            )
                            if pending_now:
                                inspection.append(
                                    subprocess.run(
                                        ("podman", "inspect", mount.container_name),
                                        text=True,
                                        capture_output=True,
                                        check=False,
                                    )
                                )
                                return
                            time.sleep(0.05)

                    inspector = threading.Thread(target=inspect_running_container)
                    inspector.start()
                    completed = run_command_supervised(
                        CommandSpec(argv, {}, base.pass_fds),
                        None,
                        None,
                        runtime,
                        mount,
                    )
                    inspector.join(timeout=10)
                    self.assertFalse(inspector.is_alive())
                    self.assertEqual(len(inspection), 1)
                    self.assertEqual(inspection[0].returncode, 0)
                    for forbidden in (
                        "family/roadmap", "repository_id", "private-key.pem",
                        str(layout.family_pending_dir), "family issue approve",
                    ):
                        self.assertNotIn(forbidden, inspection[0].stdout)
                    self.assertEqual(completed.returncode, 0)
                    pending = list_pending(layout.family_pending_dir, "demo")
                    self.assertEqual(len(pending), 1)
                    rendered = " ".join(argv)
                    for forbidden in (
                        str(layout.family_app_file),
                        str(layout.family_private_key_file),
                        str(layout.family_pending_dir),
                        "family/roadmap",
                        "repository_id",
                        "family issue approve",
                    ):
                        self.assertNotIn(forbidden, rendered)
                finally:
                    runtime.close()
                self.assertFalse(mount.socket_dir.exists())
                self.assertNotEqual(
                    subprocess.run(
                        ("podman", "container", "exists", mount.container_name),
                        check=False,
                    ).returncode,
                    0,
                )

                stale_runtime = FamilyIntakeRuntime.create(layout)
                stale_mount = stale_runtime.start()
                exited = subprocess.Popen(("/bin/true",))
                stale_pid = exited.pid
                exited.wait(timeout=5)
                try:
                    with self.assertRaisesRegex(
                        FamilyIntakeRuntimeError, "registration failed"
                    ):
                        stale_runtime.register_runtime(stale_pid)
                finally:
                    stale_runtime.close()
                self.assertFalse(stale_mount.socket_dir.exists())

                failed_runtime = FamilyIntakeRuntime.create(layout)
                failed_mount = failed_runtime.start()
                failed_base = (
                    run_codex_spec(
                        state, handover, _IMAGE, os.getuid(), os.getgid(),
                        family_mount=failed_mount,
                    )
                    if agent == "codex"
                    else run_claude_spec(
                        state, handover, _IMAGE, os.getuid(), os.getgid(),
                        HandoverRuntimeMount(Path(handover_temp) / "broker"),
                        family_mount=failed_mount,
                    )
                )
                failed_image_index = failed_base.argv.index(_IMAGE)
                failed_argv = failed_base.argv[: failed_image_index + 1] + (
                    "/bin/sh", "-c", "sleep 30",
                )

                def fail_live_broker():
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        exists = subprocess.run(
                            (
                                "podman", "container", "exists",
                                failed_mount.container_name,
                            ),
                            check=False,
                        )
                        if exists.returncode == 0:
                            failed_runtime._fail_runtime()
                            return
                        time.sleep(0.05)

                saboteur = threading.Thread(target=fail_live_broker)
                saboteur.start()
                with self.assertRaises(FamilyIntakeRuntimeError):
                    run_command_supervised(
                        CommandSpec(
                            failed_argv, {}, failed_base.pass_fds
                        ),
                        None,
                        None,
                        failed_runtime,
                        failed_mount,
                    )
                saboteur.join(timeout=10)
                self.assertFalse(saboteur.is_alive())
                try:
                    failed_runtime.close()
                except FamilyIntakeRuntimeError:
                    pass
                self.assertNotEqual(
                    subprocess.run(
                        (
                            "podman", "container", "exists",
                            failed_mount.container_name,
                        ),
                        check=False,
                    ).returncode,
                    0,
                )


if __name__ == "__main__":
    unittest.main()
