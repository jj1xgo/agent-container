from fnmatch import fnmatchcase
from pathlib import Path
import json
import re
import subprocess
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
        self.assertTrue(settings["disableAllHooks"])
        self.assertTrue(settings["allowManagedHooksOnly"])
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
        self.assertIn("ARG AGENT_CONTAINER_VERSION=0.4.0-dev.0", body)
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
                "src/agent_container/family_github_app.py",
                "src/agent_container/family_state.py",
                "!profiles/",
                "!profiles/codex/",
                "!profiles/codex/**",
                "!profiles/claude/",
                "!profiles/claude/managed-settings.json",
                "!profiles/claude/managed-mcp.json",
                "!profiles/claude/CLAUDE.md",
                "!container/",
                "!container/bin/",
                "!container/bin/codex",
                "!container/bin/claude",
                "!container/bin/git-remote-agent-broker",
                "!container/bin/agent-handover",
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
        source_files = sorted((ROOT / "src/agent_container").glob("*.py"))
        included = {
            path.name
            for path in source_files
            if containerignore_includes(str(path.relative_to(ROOT)), patterns)
        }

        self.assertTrue(
            {
                "family_intake_client.py",
                "family_intake_protocol.py",
                "family_issue.py",
            }.issubset(included)
        )
        self.assertEqual(
            {"family_github_app.py", "family_state.py"} & included,
            set(),
        )
        image_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in source_files
            if path.name in included
        )
        self.assertNotIn("family_app_file", image_source)
        self.assertNotIn("family_private_key_file", image_source)
        self.assertNotIn("FamilyInstallationTokenProvider", image_source)

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
                    "src/agent_container/family_github_app.py",
                    "src/agent_container/family_state.py",
                }:
                    self.assertFalse(containerignore_includes(path, patterns))
                else:
                    self.assertTrue(containerignore_includes(path, patterns))
