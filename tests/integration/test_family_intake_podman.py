"""Opt-in rootless Podman evidence for the family intake runtime boundary."""

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from agent_container.family_intake_runtime import FamilyIntakeRuntime
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


@unittest.skipIf(_MISSING is not None, _MISSING or "Podman prerequisite missing")
class FamilyIntakePodmanTest(unittest.TestCase):
    def test_real_runtime_ancestry_allows_one_request_and_denies_second(self) -> None:
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), TemporaryDirectory() as temp:
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
                    payload = (
                        "test \"$(env | grep -c '^AGENT_FAMILY_')\" -eq 2; "
                        "! env | grep -E 'family/roadmap|repository_id|private-key|token'; "
                        "! grep -E 'family/roadmap|private-key|/pending' /proc/self/mountinfo; "
                        "! find / -xdev -name 'private-key.pem' -print 2>/dev/null | grep .; "
                        "agent-family issue create --title T --summary S "
                        "--context C --acceptance-criterion A; "
                        "agent-family issue create --title T --summary S "
                        "--context C --acceptance-criterion A >/tmp/second 2>&1; "
                        "test $? -ne 0"
                    )
                    state = StateLayout(root, "demo")
                    handover = root / "handovers/demo"
                    for directory in (
                        state.workspace, state.codex_home, state.cache,
                        state.gh_dir, state.claude_config, handover,
                        root / "handover-broker",
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
                            HandoverRuntimeMount(root / "handover-broker"),
                            family_mount=mount,
                        )
                    )
                    image_index = base.argv.index(_IMAGE)
                    argv = base.argv[: image_index + 1] + ("/bin/sh", "-c", payload)

                    completed = run_command_supervised(
                        CommandSpec(argv, {}), None, None, runtime
                    )

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


if __name__ == "__main__":
    unittest.main()
