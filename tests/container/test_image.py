from fnmatch import fnmatchcase
from pathlib import Path
import json
import os
import re
import subprocess
import shutil
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


def containerignore_includes(path: str, patterns: list[str]) -> bool:
    included = True
    for raw_pattern in patterns:
        negated = raw_pattern.startswith("!")
        pattern = raw_pattern.removeprefix("!").rstrip("/")
        if fnmatchcase(path, pattern) or fnmatchcase(path, f"{pattern}/**"):
            included = negated
    return included


class ContainerImageContractTest(unittest.TestCase):
    def test_runtime_launcher_waits_for_registration_gate_before_exec(self) -> None:
        with TemporaryDirectory() as temp:
            marker = Path(temp) / "executed"
            read_fd, write_fd = os.pipe()
            process = subprocess.Popen(
                (
                    str(ROOT / "container/bin/agent-runtime-launcher"),
                    f"--registration-gate-fd={read_fd}",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; Path(sys.argv[1]).touch()",
                    str(marker),
                ),
                pass_fds=(read_fd,),
            )
            os.close(read_fd)
            try:
                time.sleep(0.1)
                self.assertIsNone(process.poll())
                self.assertFalse(marker.exists())
                self.assertEqual(os.write(write_fd, b"1"), 1)
            finally:
                os.close(write_fd)
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue(marker.exists())

    def test_runtime_launcher_rejects_invalid_registration_gate(self) -> None:
        malformed = subprocess.run(
            (
                str(ROOT / "container/bin/agent-runtime-launcher"),
                "--registration-gate-fd=2",
                "--",
                "/bin/true",
            ),
            check=False,
        )
        self.assertEqual(malformed.returncode, 64)

        for release in (b"", b"0"):
            with self.subTest(release=release):
                read_fd, write_fd = os.pipe()
                try:
                    if release:
                        self.assertEqual(os.write(write_fd, release), 1)
                    os.close(write_fd)
                    write_fd = -1
                    launched = subprocess.run(
                        (
                            str(ROOT / "container/bin/agent-runtime-launcher"),
                            f"--registration-gate-fd={read_fd}",
                            "--",
                            "/bin/true",
                        ),
                        pass_fds=(read_fd,),
                        check=False,
                    )
                finally:
                    os.close(read_fd)
                    if write_fd >= 0:
                        os.close(write_fd)
                self.assertEqual(launched.returncode, 70)

    def test_runtime_launcher_closes_exact_fd_before_agent_exec(self) -> None:
        with TemporaryDirectory() as temp:
            descriptor = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                launched = subprocess.run(
                    (
                        str(ROOT / "container/bin/agent-runtime-launcher"),
                        f"--close-fd={descriptor}",
                        "--",
                        sys.executable,
                        "-c",
                        (
                            "import os,sys; "
                            "sys.exit(1 if os.path.exists('/proc/self/fd/' + sys.argv[1]) else 0)"
                        ),
                        str(descriptor),
                    ),
                    pass_fds=(descriptor,),
                    check=False,
                )
            finally:
                os.close(descriptor)
        self.assertEqual(launched.returncode, 0)

    def test_image_installs_exact_managed_claude_handover_instructions(self) -> None:
        instructions_path = ROOT / "profiles/claude/CLAUDE.md"
        instructions = instructions_path.read_text(encoding="utf-8")

        self.assertEqual(instructions.count("agent-handover create --title"), 1)
        required_sections = (
            "## 作業の目的",
            "## 現在地",
            "## 決定事項と理由",
            "## 変更したファイル・commit・PR",
            "## 検証結果",
            "## 未解決事項とリスク",
            "## 次の一手",
        )
        self.assertEqual(
            [
                line
                for line in instructions.splitlines()
                if line.startswith("## ")
            ],
            list(required_sections),
        )
        for required_text in (
            "stdin",
            "Git status",
            "直近 commit",
            "実行済み test",
            "credential",
            "環境値",
            "transcript 全文",
            "sandbox",
            "mount",
            "fallback",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, instructions)

        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY --chmod=0644 profiles/claude/CLAUDE.md /etc/claude-code/CLAUDE.md",
            body,
        )
        self.assertIn("RUN install -d -m 0755 /run/agent-family", body)

    def test_image_copies_exact_claude_managed_policy(self) -> None:
        settings_path = ROOT / "profiles/claude/managed-settings.json"
        mcp_path = ROOT / "profiles/claude/managed-mcp.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["enableWeakerNestedSandbox"])
        self.assertEqual(
            settings["sandbox"].get("network"), {"allowAllUnixSockets": True}
        )
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertIn(
            {"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
            settings["sandbox"]["credentials"]["envVars"],
        )
        self.assertIn(
            {"path": "/run/secrets/claude-oauth-token", "mode": "deny"},
            settings["sandbox"]["credentials"]["files"],
        )
        self.assertIn(
            "Read(//run/secrets/claude-oauth-token)",
            settings["permissions"]["deny"],
        )
        self.assertEqual(
            settings["permissions"]["disableBypassPermissionsMode"], "disable"
        )
        self.assertNotIn("disableAllHooks", settings)
        self.assertTrue(settings["allowManagedHooksOnly"])
        self.assertNotIn("hooks", settings)
        self.assertEqual(
            settings["statusLine"],
            {
                "type": "command",
                "command": "bash /etc/claude-code/statusline.sh",
                "padding": 0,
            },
        )
        self.assertEqual(settings["allowedMcpServers"], [])
        self.assertTrue(settings["allowManagedMcpServersOnly"])
        self.assertEqual(
            json.loads(mcp_path.read_text(encoding="utf-8")), {"mcpServers": {}}
        )

        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY --chmod=0644 profiles/claude/managed-settings.json /etc/claude-code/managed-settings.json",
            body,
        )
        self.assertIn(
            "COPY --chmod=0644 profiles/claude/managed-mcp.json /etc/claude-code/managed-mcp.json",
            body,
        )
        self.assertIn(
            "COPY --chmod=0644 profiles/claude/statusline.sh /etc/claude-code/statusline.sh",
            body,
        )

    def test_managed_claude_statusline_only_renders_stdin_session_json(self) -> None:
        statusline_path = ROOT / "profiles/claude/statusline.sh"
        script = statusline_path.read_text(encoding="utf-8")

        self.assertFalse(statusline_path.stat().st_mode & 0o111)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("input=$(cat)", script)
        for forbidden in (
            "printenv",
            "environ",
            "/run/secrets",
            "OAUTH_TOKEN",
            "API_KEY",
            "AUTH_TOKEN",
            "curl",
            "wget",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)

    def test_image_installs_claude_sandbox_dependencies(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        install = re.search(
            r"apt-get install -y --no-install-recommends\s+"
            r"(?P<packages>.*?)\s+&& rm -rf /var/lib/apt/lists/\*",
            body,
            re.DOTALL,
        )

        self.assertIsNotNone(install)
        installed = set(install.group("packages").replace("\\", "").split())
        self.assertEqual(
            {
                "bubblewrap",
                "ca-certificates",
                "curl",
                "git",
                "jq",
                "libatomic1",
                "python3",
                "python3-pip",
                "ripgrep",
                "socat",
                "xz-utils",
            }
            - installed,
            set(),
        )

    def test_image_installs_the_pinned_runtime_linter(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        requirement = (ROOT / "requirements-lint.txt").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertEqual(requirement, "ruff==0.16.4\n")
        self.assertIn("COPY requirements-lint.txt /opt/agent-container/", body)
        self.assertIn(
            "python3 -m pip install --disable-pip-version-check --no-deps",
            body,
        )
        self.assertIn("-r /opt/agent-container/requirements-lint.txt", body)
        self.assertIn(
            'podman run --rm "$BASE_IMAGE" python3 -m pip --version', workflow
        )
        self.assertIn(
            'test "$(podman run --rm "$BASE_IMAGE" python3 -m ruff --version)" = "ruff 0.16.4"',
            workflow,
        )
        self.assertIn(
            'podman run --rm "$BASE_IMAGE" rg --version',
            workflow,
        )

    def test_image_bootstraps_ca_then_uses_https_for_debian_packages(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")

        ca_install = body.index(
            "apt-get install -y --no-install-recommends ca-certificates"
        )
        https_source = body.index("URIs: http://deb.debian.org")
        main_install = body.index("bubblewrap ca-certificates curl git")

        self.assertLess(ca_install, https_source)
        self.assertLess(https_source, main_install)
        self.assertIn("URIs: https://deb.debian.org", body)
        self.assertIn(
            "grep -qx 'URIs: https://deb.debian.org/debian'", body
        )
        self.assertIn(
            "grep -qx 'URIs: https://deb.debian.org/debian-security'", body
        )
        self.assertIn("! grep -q '^URIs: http://'", body)
        self.assertGreaterEqual(body.count("apt-get update"), 2)

    def test_image_installs_checksum_verified_official_github_cli(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertNotIn("api.github.com/repos/cli/cli/releases/latest", body)
        self.assertIn("https://github.com/cli/cli/releases/latest", body)
        self.assertIn("%{url_effective}", body)
        self.assertIn("https://github.com/cli/cli/releases/download/", body)
        self.assertIn('gh_${gh_version}_checksums.txt', body)
        self.assertRegex(body, r"sha256sum\s+--check\s+--strict")

    def test_image_installs_latest_agent_clis_and_runs_as_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("FROM docker.io/library/debian:testing-slim", body)
        self.assertNotIn("FROM docker.io/library/node:", body)
        self.assertIn("ARG NODE_VERSION=latest", body)
        self.assertIn("ARG CODEX_VERSION=latest", body)
        self.assertIn("ARG CLAUDE_VERSION=latest", body)
        self.assertIn("ARG AGENT_CLI_CACHEBUST=0", body)
        self.assertIn("ARG AGENT_CONTAINER_VERSION\n", body)
        self.assertNotIn("ARG AGENT_CONTAINER_VERSION=", body)
        self.assertIn("@openai/codex@${CODEX_VERSION}", body)
        self.assertIn("@anthropic-ai/claude-code@${CLAUDE_VERSION}", body)
        self.assertIn(
            "PATH=/opt/agent-node/bin:$PATH /opt/agent-node/bin/npm install --global",
            body,
        )
        self.assertIn("https://nodejs.org/dist/", body)
        self.assertIn("SHASUMS256.txt", body)
        self.assertIn("/opt/agent-node", body)
        self.assertIn("DISABLE_UPDATES=1", body)
        self.assertIn("USER agent", body)
        self.assertIn("WORKDIR /workspace", body)
        self.assertIn("COPY src /opt/agent-container/src", body)
        self.assertIn("PYTHONPATH=/opt/agent-container/src", body)
        self.assertIn("AGENT_CONTAINER_VERSION=${AGENT_CONTAINER_VERSION}", body)
        self.assertIn("COPY profiles/codex /opt/agent-container/profiles/codex", body)
        self.assertIn(
            "COPY --chmod=0755 container/bin/agent-handover /usr/local/bin/agent-handover",
            body,
        )
        self.assertIn(
            "COPY --chmod=0755 container/bin/agent-family /usr/local/bin/agent-family",
            body,
        )
        self.assertIn(
            "COPY --chmod=0755 container/bin/agent-egress-adapter /usr/local/bin/agent-egress-adapter",
            body,
        )
        self.assertIn(
            "COPY --chmod=0755 container/bin/agent-egress-runtime /usr/local/bin/agent-egress-runtime",
            body,
        )
        self.assertIn(
            "COPY --chmod=0755 container/bin/agent-runtime-launcher "
            "/usr/local/bin/agent-runtime-launcher",
            body,
        )
        launcher = (ROOT / "container/bin/agent-runtime-launcher").read_text(
            "utf-8"
        )
        self.assertIn("os.close", launcher)
        self.assertIn("os.execvp", launcher)
        self.assertNotIn("os.environ", launcher)
        self.assertIn(
            "COPY container/profile.d/10-agent-node.sh /etc/profile.d/10-agent-node.sh",
            body,
        )
        self.assertNotRegex(body, r"(?m)^COPY\s+.*(?:credentials|oauth-token)")

        profile = (ROOT / "container/profile.d/10-agent-node.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("PATH=/opt/agent-node/bin:$PATH", profile)

        codex_wrapper = (ROOT / "container/bin/codex").read_text(encoding="utf-8")
        self.assertIn(
            "exec /opt/agent-node/bin/node /opt/agent-node/bin/codex",
            codex_wrapper,
        )
        self.assertNotIn("/usr/bin/env node", codex_wrapper)

        for script_name, module in (
            ("agent-egress-adapter", "agent_container.egress_adapter"),
            ("agent-egress-runtime", "agent_container.egress_runtime"),
        ):
            script = (ROOT / "container/bin" / script_name).read_text(encoding="utf-8")
            self.assertIn("PYTHONPATH=/opt/agent-container/src", script)
            self.assertIn(f"python3 -P -m {module}", script)

        claude_wrapper = (ROOT / "container/bin/claude").read_text(encoding="utf-8")
        self.assertIn("exec /opt/agent-node/bin/claude", claude_wrapper)
        self.assertNotIn("/opt/agent-node/bin/node", claude_wrapper)

        broker_wrapper = (ROOT / "container/bin/git-remote-agent-broker").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "exec python3 -m agent_container.git_remote_helper_cli",
            broker_wrapper,
        )

        family_wrapper = (ROOT / "container/bin/agent-family").read_text(
            encoding="utf-8"
        )
        self.assertIn("agent_container.family_intake_client", family_wrapper)
        self.assertIn("main()", family_wrapper)
        self.assertNotIn("agentctl", family_wrapper)
        self.assertNotIn("approve", family_wrapper)
        self.assertNotIn("credential", family_wrapper)

    def test_image_omits_family_approval_and_credential_material(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertNotIn("agentctl family", body)
        self.assertNotIn("private-key.pem", body)
        self.assertNotIn("family-app", body)

    def test_image_creates_fixed_agent_identity(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertRegex(body, r"groupadd[^\n]*--gid\s+1000\s+agent")
        self.assertRegex(body, r"useradd[^\n]*--uid\s+1000[^\n]*--gid\s+1000")
        self.assertNotIn("groupmod", body)
        self.assertNotIn("usermod", body)

    def test_containerignore_is_an_allowlist_for_build_inputs_only(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            patterns,
            [
                "**",
                "!Containerfile",
                "!requirements-lint.txt",
                "!src/",
                "!src/**",
                "src/agent_container/family_cli.py",
                "src/agent_container/family_github_app.py",
                "src/agent_container/family_issue_create.py",
                "src/agent_container/family_state.py",
                "src/agent_container/family_intake_broker.py",
                "src/agent_container/family_intake_runtime.py",
                "src/agent_container/family_intake_transport.py",
                "src/agent_container/family_pending.py",
                "!profiles/",
                "!profiles/codex/",
                "!profiles/codex/**",
                "!profiles/claude/",
                "!profiles/claude/managed-settings.json",
                "!profiles/claude/managed-mcp.json",
                "!profiles/claude/CLAUDE.md",
                "!profiles/claude/statusline.sh",
                "!container/",
                "!container/bin/",
                "!container/bin/codex",
                "!container/bin/claude",
                "!container/bin/git-remote-agent-broker",
                "!container/bin/agent-handover",
                "!container/bin/agent-runtime-launcher",
                "!container/profile.d/",
                "!container/profile.d/10-agent-node.sh",
                "**/auth.json",
                "**/hosts.yml",
                "**/.git",
                "**/.git/**",
                "**/.worktrees",
                "**/.worktrees/**",
                "**/.codex",
                "**/.codex/**",
                "**/__pycache__",
                "**/__pycache__/**",
                "**/.cache",
                "**/.cache/**",
                "**/*.pyc",
            ],
        )

        sensitive_nested_paths = (
            "src/fixture/auth.json",
            "profiles/codex/nested/auth.json",
            "src/fixture/hosts.yml",
            "profiles/codex/.git/config",
            "src/.worktrees/topic/index",
            "profiles/codex/.codex/session.json",
            "src/agent_container/__pycache__/state.cpython-314.pyc",
            "profiles/codex/generated.pyc",
            "src/agent_container/.cache/archive.bin",
        )
        for path in sensitive_nested_paths:
            with self.subTest(path=path):
                self.assertFalse(containerignore_includes(path, patterns))

    def test_effective_image_source_set_excludes_host_only_family_modules(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        source_files = sorted((ROOT / "src/agent_container").rglob("*.py"))
        included = {
            path.relative_to(ROOT).as_posix()
            for path in source_files
            if containerignore_includes(str(path.relative_to(ROOT)), patterns)
        }

        self.assertTrue(
            {
                "src/agent_container/family_intake_client.py",
                "src/agent_container/family_intake_protocol.py",
                "src/agent_container/family_issue.py",
            }.issubset(included)
        )
        self.assertEqual(
            {
                "src/agent_container/family_cli.py",
                "src/agent_container/family_github_app.py",
                "src/agent_container/family_state.py",
                "src/agent_container/family_intake_broker.py",
                "src/agent_container/family_intake_runtime.py",
                "src/agent_container/family_intake_transport.py",
                "src/agent_container/family_pending.py",
            }
            & included,
            set(),
        )
        image_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in source_files
            if path.relative_to(ROOT).as_posix() in included
        )
        self.assertNotIn("family_app_file", image_source)
        self.assertNotIn("family_private_key_file", image_source)
        self.assertNotIn("FamilyInstallationTokenProvider", image_source)
        for module in ("agentctl.py", "podman.py"):
            source = (ROOT / "src/agent_container" / module).read_text("utf-8")
            top_level = source.split("\ndef ", 1)[0]
            self.assertNotIn(
                "from agent_container.family_state import", top_level
            )
            self.assertNotIn(
                "from agent_container.family_intake_runtime import", top_level
            )

    def test_effective_image_tree_imports_host_entrypoints_without_host_modules(self) -> None:
        patterns = (ROOT / ".containerignore").read_text("utf-8").splitlines()
        with TemporaryDirectory() as temp:
            target = Path(temp) / "src" / "agent_container"
            target.mkdir(parents=True)
            for source in (ROOT / "src/agent_container").glob("*.py"):
                relative = source.relative_to(ROOT).as_posix()
                if containerignore_includes(relative, patterns):
                    shutil.copy2(source, target / source.name)
            for forbidden in (
                "family_state.py",
                "family_intake_runtime.py",
                "family_intake_broker.py",
                "family_intake_transport.py",
                "family_pending.py",
            ):
                self.assertFalse((target / forbidden).exists())
            probe = subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "import sys; sys.path.insert(0, 'src'); "
                        "import agent_container.agentctl; "
                        "import agent_container.podman"
                    ),
                ),
                cwd=temp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual((probe.returncode, probe.stderr), (0, ""))

    def test_containerignore_includes_every_tracked_copy_input(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        tracked = subprocess.run(
            (
                "git",
                "ls-files",
                "--",
                "Containerfile",
                "requirements-lint.txt",
                "src",
                "profiles/codex",
                "container/bin",
            ),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

        self.assertTrue(tracked)
        for path in tracked:
            with self.subTest(path=path):
                if path in {
                    "src/agent_container/family_cli.py",
                    "src/agent_container/family_github_app.py",
                    "src/agent_container/family_issue_create.py",
                    "src/agent_container/family_state.py",
                    "src/agent_container/family_intake_broker.py",
                    "src/agent_container/family_intake_runtime.py",
                    "src/agent_container/family_intake_transport.py",
                    "src/agent_container/family_pending.py",
                }:
                    self.assertFalse(containerignore_includes(path, patterns))
                else:
                    self.assertTrue(containerignore_includes(path, patterns))
