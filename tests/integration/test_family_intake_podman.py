"""Opt-in rootless Podman evidence for the family intake runtime boundary."""

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_intake_runtime import FamilyIntakeRuntimeError
from agent_container.family_runtime_mount import FamilyRuntimeMount
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
from agent_container.egress_broker_runtime import EgressRuntimeMount


_IMAGE = os.environ.get("AGENT_FAMILY_TEST_IMAGE", "")
_ANCESTOR_DEPTH = 8


def _ancestor_sentinel_reachable(
    descriptors: tuple[int, ...], sentinel_name: str
) -> bool:
    """Model the container probe's bounded descriptor-relative ancestor search."""

    if re.fullmatch(r"\.family-ancestor-[0-9a-f]{32}", sentinel_name) is None:
        raise ValueError("ancestor sentinel name is invalid")
    for descriptor in descriptors:
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                continue
        except OSError:
            continue
        for depth in range(_ANCESTOR_DEPTH + 1):
            relative = "../" * depth + sentinel_name
            try:
                os.stat(relative, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            return True
    return False


def _fd_probe_command(sentinel_name: str) -> str:
    if re.fullmatch(r"\.family-ancestor-[0-9a-f]{32}", sentinel_name) is None:
        raise ValueError("ancestor sentinel name is invalid")
    code = "\n".join(
        (
            "import os, stat, sys",
            "sentinel = sys.argv[1]",
            "target = '/run/agent-family/intake.sock'",
            "if not stat.S_ISSOCK(os.stat(target, follow_symlinks=False).st_mode): raise SystemExit(93)",
            "mounts = [line for line in open('/proc/self/mountinfo', encoding='ascii') if f' {target} ' in line]",
            "if len(mounts) != 1: raise SystemExit(93)",
            "for text in os.listdir('/proc/self/fd'):",
            "    try: fd = int(text); details = os.fstat(fd)",
            "    except (OSError, ValueError): continue",
            "    if not stat.S_ISDIR(details.st_mode): continue",
            "    try: linked = os.readlink(f'/proc/self/fd/{fd}')",
            "    except OSError: linked = ''",
            "    if '/run/agent-family' in linked or '/family/' in linked or '/state/' in linked: raise SystemExit(94)",
            f"    for depth in range({_ANCESTOR_DEPTH + 1}):",
            "        try: os.stat('../' * depth + sentinel, dir_fd=fd, follow_symlinks=False)",
            "        except OSError: continue",
            "        raise SystemExit(94)",
        )
    )
    return f"python3 -c {shlex.quote(code)} {shlex.quote(sentinel_name)}"


def _probe_script(
    marker_id: str,
    sentinel_name: str = ".family-ancestor-0123456789abcdef0123456789abcdef",
) -> str:
    if re.fullmatch(r"[0-9a-f]{16}", marker_id) is None:
        raise ValueError("inspection marker id is invalid")
    ready = f"/workspace/.family-inspect-{marker_id}-ready"
    done = f"/workspace/.family-inspect-{marker_id}-done"
    return (
    "set -eu; "
    "test \"$(env | grep -c '^AGENT_FAMILY_')\" -eq 2; "
    "! env | grep -E 'family/roadmap|repository_id|private-key|token'; "
    "! grep -E 'family/roadmap|private-key|/pending' /proc/self/mountinfo; "
    "! find / -xdev -name 'private-key.pem' -print 2>/dev/null | grep .; "
    f"{_fd_probe_command(sentinel_name)}; "
    "agent-family issue create --title T --summary S "
    "--context C --acceptance-criterion A; "
    f": > {ready}; "
    f"deadline=100; while ! test -e {done}; do "
    "deadline=$((deadline - 1)); test \"$deadline\" -gt 0 || exit 92; "
    "sleep 0.05; done; "
    f"rm -f {ready} {done}; "
    "if agent-family issue create --title T --summary S "
    "--context C --acceptance-criterion A >/tmp/second 2>&1; "
    "then exit 91; fi"
    )


_PROBE_SCRIPT = _probe_script("0123456789abcdef")


def _marker_paths(workspace: Path, marker_id: str) -> tuple[Path, Path]:
    if not workspace.is_absolute() or workspace.is_symlink():
        raise ValueError("inspection workspace is invalid")
    _probe_script(marker_id)
    paths = tuple(
        workspace / f".family-inspect-{marker_id}-{suffix}"
        for suffix in ("ready", "done")
    )
    for path in paths:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        raise ValueError("inspection marker collision")
    return paths


def _marker_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _validate_released_marker(path: Path) -> None:
    try:
        details = os.lstat(path)
    except FileNotFoundError as error:
        raise ValueError("inspection marker is unsafe") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or details.st_size != 0
    ):
        raise ValueError("inspection marker is unsafe")


def _release_marker(path: Path) -> None:
    if _marker_exists(path):
        _validate_released_marker(path)
        return
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        _validate_released_marker(path)
        return
    os.close(descriptor)


def _podman_prerequisite() -> str | None:
    if any(os.environ.get(name) for name in ("CONTAINER_HOST", "CONTAINER_CONNECTION")):
        return "a local (non-remote) Podman service is required"
    if shutil.which("podman") is None:
        return "rootless Podman executable is unavailable"
    try:
        version = subprocess.run(
            ("podman", "--version"), text=True, capture_output=True,
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Podman prerequisite probes did not complete"
    match = re.fullmatch(
        r"podman version ([0-9]+)\.([0-9]+)(?:\.[0-9]+)?",
        version.stdout.strip(),
    )
    if version.returncode != 0 or match is None or (
        int(match.group(1)), int(match.group(2))
    ) < (5, 8):
        return "local Podman 5.8 or newer is required"
    try:
        probe = subprocess.run(
            ("podman", "info", "--format", "{{.Host.Security.Rootless}}"),
            text=True, capture_output=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Podman prerequisite probes did not complete"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return "a working rootless Podman service is unavailable"
    try:
        runtime = subprocess.run(
            ("podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"),
            text=True, capture_output=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Podman prerequisite probes did not complete"
    if runtime.returncode != 0 or runtime.stdout.strip() != "crun":
        return "the local crun OCI runtime is required"
    try:
        connections = subprocess.run(
            ("podman", "system", "connection", "list", "--format", "json"),
            text=True, capture_output=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Podman prerequisite probes did not complete"
    try:
        decoded = json.loads(connections.stdout)
    except (json.JSONDecodeError, TypeError):
        return "Podman local connection state is unavailable"
    if connections.returncode != 0 or type(decoded) is not list or any(
        type(item) is not dict
        or type(item.get("Default", item.get("default", False))) is not bool
        or item.get("Default", item.get("default", False))
        for item in decoded
    ):
        return "a local (non-remote) Podman service is required"
    if not _IMAGE:
        return "AGENT_FAMILY_TEST_IMAGE is not configured"
    try:
        image = subprocess.run(
            ("podman", "image", "exists", _IMAGE), check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return "instrumented image prerequisite probe did not complete"
    if image.returncode != 0:
        return "the instrumented family intake image is unavailable"
    return None


_MISSING = (
    _podman_prerequisite()
    if os.environ.get("AGENT_CONTAINER_RUN_PODMAN_INTEGRATION") == "1"
    else "AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 is not configured"
)


class FamilyIntakePodmanFixtureTest(unittest.TestCase):
    def test_concurrent_marker_release_is_idempotent_after_safe_create(self) -> None:
        with TemporaryDirectory() as temp:
            marker = Path(temp) / "done"
            barrier = threading.Barrier(2)
            real_open = os.open
            failures = []

            def synchronized_open(path, flags, mode=0o777):
                if Path(path) == marker and flags & os.O_EXCL:
                    barrier.wait(timeout=5)
                return real_open(path, flags, mode)

            def release() -> None:
                try:
                    _release_marker(marker)
                except Exception as error:
                    failures.append(error)

            with mock.patch.object(os, "open", side_effect=synchronized_open):
                threads = [threading.Thread(target=release) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            details = marker.lstat()
            self.assertTrue(stat.S_ISREG(details.st_mode))
            self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
            self.assertEqual(details.st_nlink, 1)

    def test_marker_release_rejects_existing_symlink_and_nonregular_entry(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.write_text("", encoding="ascii")
            symlink = root / "symlink"
            symlink.symlink_to(target)
            directory = root / "directory"
            directory.mkdir()

            for unsafe in (symlink, directory):
                with self.subTest(kind=unsafe.name), self.assertRaisesRegex(
                    ValueError, "marker is unsafe"
                ):
                    _release_marker(unsafe)

    def test_remote_environment_skips_before_any_podman_probe(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CONTAINER_HOST": "ssh://example.invalid/run/podman.sock"},
            clear=True,
        ), mock.patch("shutil.which") as which, mock.patch(
            "subprocess.run"
        ) as run:
            reason = _podman_prerequisite()
        self.assertEqual(reason, "a local (non-remote) Podman service is required")
        which.assert_not_called()
        run.assert_not_called()

    def test_old_podman_stops_prerequisite_probes_at_version(self) -> None:
        version = subprocess.CompletedProcess(
            ("podman", "--version"), 0, stdout="podman version 5.7.9\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "shutil.which", return_value="/usr/bin/podman"
        ), mock.patch("subprocess.run", return_value=version) as run:
            reason = _podman_prerequisite()
        self.assertEqual(reason, "local Podman 5.8 or newer is required")
        self.assertEqual(run.call_count, 1)

    def test_marker_paths_are_unique_workspace_local_and_reject_collisions(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            ready, done = _marker_paths(workspace, "fedcba9876543210")
            self.assertEqual(ready.parent, workspace)
            self.assertEqual(done.parent, workspace)
            outside = workspace / "outside"
            outside.write_text("x", encoding="ascii")
            ready.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "collision"):
                _marker_paths(workspace, "fedcba9876543210")

    def test_instrumented_script_is_strict_and_shell_syntax_is_valid(self) -> None:
        checked = subprocess.run(
            ("/bin/sh", "-n", "-c", _PROBE_SCRIPT),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual((checked.returncode, checked.stderr), (0, ""))
        self.assertTrue(_PROBE_SCRIPT.startswith("set -eu;"))
        self.assertIn("if agent-family issue create", _PROBE_SCRIPT)
        self.assertIn("-ready", _PROBE_SCRIPT)
        self.assertIn("-done", _PROBE_SCRIPT)
        self.assertIn("/workspace/.family-inspect-", _PROBE_SCRIPT)
        self.assertNotIn("/run/agent-family/inspect", _PROBE_SCRIPT)
        self.assertNotIn("; test $?", _PROBE_SCRIPT)
        self.assertNotIn("sleep 2", _PROBE_SCRIPT)
        self.assertIn("deadline=", _PROBE_SCRIPT)
        self.assertIn("exit 92", _PROBE_SCRIPT)
        self.assertIn("/proc/self/fd", _PROBE_SCRIPT)
        self.assertIn("os.fstat", _PROBE_SCRIPT)
        self.assertIn("intake.sock", _PROBE_SCRIPT)
        self.assertIn("SystemExit(94)", _PROBE_SCRIPT)

    def test_both_real_specs_have_pinned_fd_shape_without_podman(self) -> None:
        state = StateLayout(Path("/state"), "demo")
        capability = "A" * 43
        mount = object.__new__(FamilyRuntimeMount)
        object.__setattr__(
            mount,
            "socket_dir",
            Path("/state/family/intake/r/2a97516c354b/0123456789abcdef"),
        )
        object.__setattr__(mount, "capability", capability)
        object.__setattr__(mount, "environment", {
            "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
            "AGENT_FAMILY_CAPABILITY": capability,
        })
        object.__setattr__(mount, "_directory_identity", (1, 2))
        object.__setattr__(mount, "_socket_identity", (1, 3))
        object.__setattr__(mount, "_directory_descriptor", 77)
        object.__setattr__(mount, "_socket_descriptor", 78)
        with mock.patch.object(type(mount), "revalidate"):
            egress_codex = EgressRuntimeMount(
                Path("/state/egress/codex"), "demo", "codex"
            )
            egress_claude = EgressRuntimeMount(
                Path("/state/egress/claude"), "demo", "claude"
            )
            specs = (
                run_codex_spec(
                    state, Path("/handover/demo"), "image", os.getuid(),
                    os.getgid(), family_mount=mount,
                ),
                run_claude_spec(
                    state, Path("/handover/demo"), "image", os.getuid(),
                    os.getgid(), HandoverRuntimeMount(Path("/handover/broker")),
                    family_mount=mount,
                ),
                run_codex_spec(
                    state, Path("/handover/demo"), "image", os.getuid(),
                    os.getgid(), family_mount=mount, egress=egress_codex,
                ),
                run_claude_spec(
                    state, Path("/handover/demo"), "image", os.getuid(),
                    os.getgid(), HandoverRuntimeMount(Path("/handover/broker")),
                    family_mount=mount, egress=egress_claude,
                ),
            )
        for spec in specs:
            self.assertEqual(spec.pass_fds, ())
            self.assertNotIn("--preserve-fd=77", spec.argv)
            self.assertEqual(
                spec.argv.count(
                    f"type=bind,src={mount.socket_path},"
                    "dst=/run/agent-family/intake.sock"
                ),
                1,
            )
            image_index = spec.argv.index("image")
            self.assertEqual(
                spec.argv[image_index + 1 : image_index + 4],
                ("agent-runtime-launcher", "--registration-stop", "--"),
            )
            if "agent-egress-runtime" in spec.argv:
                self.assertGreater(
                    spec.argv.index("agent-egress-runtime"), image_index + 3
                )


@unittest.skipIf(_MISSING is not None, _MISSING or "Podman prerequisite missing")
class FamilyIntakePodmanTest(unittest.TestCase):
    def test_preserved_run_directory_fd_negative_fixture_reaches_sentinel(self) -> None:
        with TemporaryDirectory() as temp:
            family = Path(temp) / "family"
            run_dir = family / "intake" / "r" / "project" / "run"
            run_dir.mkdir(parents=True)
            sentinel_name = ".family-ancestor-" + secrets.token_hex(16)
            (family / sentinel_name).write_bytes(b"")
            socket_path = run_dir / "intake.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            directory_fd = os.open(
                run_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                completed = subprocess.run(
                    (
                        "podman",
                        "run",
                        "--rm",
                        "--network=none",
                        f"--preserve-fd={directory_fd}",
                        "--mount",
                        "type=bind,src=" + str(socket_path)
                        + ",dst=/run/agent-family/intake.sock",
                        _IMAGE,
                        "/bin/sh",
                        "-c",
                        _fd_probe_command(sentinel_name),
                    ),
                    pass_fds=(directory_fd,),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            finally:
                os.close(directory_fd)
                listener.close()

        self.assertEqual(
            completed.returncode,
            94,
            "inherited directory FD did not expose the ancestor sentinel; "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    def test_real_runtime_ancestry_allows_one_request_and_denies_second(self) -> None:
        variants = tuple(
            (agent, with_egress)
            for agent in ("codex", "claude")
            for with_egress in (False, True)
        )
        for agent, with_egress in variants:
            with (
                self.subTest(agent=agent, with_egress=with_egress),
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
                sentinel_name = ".family-ancestor-" + secrets.token_hex(16)
                sentinel_path = layout.family_root / sentinel_name
                sentinel_descriptor = os.open(
                    sentinel_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                os.close(sentinel_descriptor)
                runtime = FamilyIntakeRuntime.create(layout)
                mount = runtime.start()
                process_reads = []
                assert runtime.session is not None
                process_reader = runtime.session.process_reader

                def record_process_read(pid):
                    process_reads.append(pid)
                    return process_reader(pid)

                runtime.session.process_reader = record_process_read
                inspector = None
                marker_paths = ()
                try:
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
                    marker_id = secrets.token_hex(8)
                    ready_marker, done_marker = _marker_paths(
                        state.workspace, marker_id
                    )
                    marker_paths = (ready_marker, done_marker)
                    payload = _probe_script(marker_id, sentinel_name)
                    egress = None
                    if with_egress:
                        egress_dir = root / "egress-fixture" / agent
                        egress_dir.mkdir(parents=True, mode=0o700)
                        egress = EgressRuntimeMount(egress_dir, "demo", agent)
                    base = (
                        run_codex_spec(
                            state, handover, _IMAGE, os.getuid(), os.getgid(),
                            family_mount=mount, egress=egress,
                        )
                        if agent == "codex"
                        else run_claude_spec(
                            state, handover, _IMAGE, os.getuid(), os.getgid(),
                            HandoverRuntimeMount(Path(handover_temp) / "broker"),
                            family_mount=mount, egress=egress,
                        )
                    )
                    image_index = base.argv.index(_IMAGE)
                    launcher_end = base.argv.index("--", image_index + 1)
                    argv = base.argv[: launcher_end + 1] + (
                        "/bin/sh", "-c", payload,
                    )

                    inspection = []
                    container_name = (
                        egress.container_name
                        if egress is not None
                        else mount.container_name
                    )

                    def inspect_running_container():
                        deadline = time.monotonic() + 5
                        try:
                            while time.monotonic() < deadline:
                                if _marker_exists(ready_marker):
                                    inspection.append(
                                        subprocess.run(
                                            ("podman", "inspect", container_name),
                                            text=True, capture_output=True,
                                            check=False, timeout=5,
                                        )
                                    )
                                    return
                                time.sleep(0.05)
                        finally:
                            _release_marker(done_marker)

                    inspector = threading.Thread(target=inspect_running_container)
                    inspector.start()
                    try:
                        try:
                            completed = run_command_supervised(
                                CommandSpec(argv, {}, base.pass_fds),
                                None, egress, runtime, mount,
                            )
                        except (RuntimeError, subprocess.CalledProcessError) as error:
                            raise AssertionError(
                                "family peer validation process reads: "
                                + str(len(process_reads))
                            ) from error
                    finally:
                        _release_marker(done_marker)
                        inspector.join(timeout=10)
                    self.assertFalse(inspector.is_alive())
                    self.assertEqual(len(inspection), 1)
                    self.assertEqual(inspection[0].returncode, 0)
                    inspected = json.loads(inspection[0].stdout)
                    self.assertIs(type(inspected), list)
                    self.assertEqual(len(inspected), 1)
                    family_mounts = [
                        item
                        for item in inspected[0].get("Mounts", ())
                        if item.get("Destination", "").startswith(
                            "/run/agent-family"
                        )
                    ]
                    self.assertEqual(len(family_mounts), 1)
                    self.assertEqual(
                        family_mounts[0].get("Destination"),
                        "/run/agent-family/intake.sock",
                    )
                    self.assertEqual(family_mounts[0].get("Type"), "bind")
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
                    if inspector is not None and inspector.is_alive():
                        if len(marker_paths) == 2:
                            _release_marker(marker_paths[1])
                        inspector.join(timeout=10)
                    for marker in marker_paths:
                        marker.unlink(missing_ok=True)
                    runtime.close()
                    sentinel_path.unlink(missing_ok=True)
                self.assertFalse(mount.socket_dir.exists())
                self.assertNotEqual(
                    subprocess.run(
                        ("podman", "container", "exists", mount.container_name),
                        check=False,
                        timeout=5,
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
                        family_mount=failed_mount, egress=egress,
                    )
                    if agent == "codex"
                    else run_claude_spec(
                        state, handover, _IMAGE, os.getuid(), os.getgid(),
                        HandoverRuntimeMount(Path(handover_temp) / "broker"),
                        family_mount=failed_mount, egress=egress,
                    )
                )
                failed_image_index = failed_base.argv.index(_IMAGE)
                failed_launcher_end = failed_base.argv.index(
                    "--", failed_image_index + 1
                )
                failed_argv = failed_base.argv[: failed_launcher_end + 1] + (
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
                            timeout=5,
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
                        egress,
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
                        timeout=5,
                    ).returncode,
                    0,
                )


if __name__ == "__main__":
    unittest.main()
