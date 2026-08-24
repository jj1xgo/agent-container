from fnmatch import fnmatchcase
from pathlib import Path
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
    def test_image_installs_claude_sandbox_dependencies(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        install = re.search(
            r"apt-get install -y --no-install-recommends (?P<packages>[^\n]+)",
            body,
        )

        self.assertIsNotNone(install)
        installed = set(install.group("packages").split())
        self.assertEqual({"bubblewrap", "socat"} - installed, set())

    def test_image_installs_latest_agent_clis_and_runs_as_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("ARG CODEX_VERSION=latest", body)
        self.assertIn("ARG CLAUDE_VERSION=latest", body)
        self.assertIn("ARG AGENT_CLI_CACHEBUST=0", body)
        self.assertIn("@openai/codex@${CODEX_VERSION}", body)
        self.assertIn("@anthropic-ai/claude-code@${CLAUDE_VERSION}", body)
        self.assertIn("DISABLE_UPDATES=1", body)
        self.assertIn("USER agent", body)
        self.assertIn("WORKDIR /workspace", body)
        self.assertIn("COPY src /opt/agent-container/src", body)
        self.assertIn("PYTHONPATH=/opt/agent-container/src", body)
        self.assertIn("COPY profiles/codex /opt/agent-container/profiles/codex", body)
        self.assertNotRegex(body, r"(?m)^COPY\s+.*(?:credentials|oauth-token)")

    def test_image_reuses_base_node_identity_for_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertNotRegex(body, r"useradd[^\n]*--uid\s+1000")
        self.assertNotRegex(body, r"groupadd[^\n]*--gid\s+1000")
        self.assertRegex(body, r"groupmod[^\n]*--new-name\s+agent\s+node")
        self.assertRegex(
            body,
            re.compile(
                r"usermod[^\n]*--login\s+agent[^\n]*"
                r"--home\s+/home/agent[^\n]*--move-home[^\n]*\snode"
            ),
        )

    def test_containerignore_is_an_allowlist_for_build_inputs_only(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            patterns,
            [
                "**",
                "!Containerfile",
                "!src/",
                "!src/**",
                "!profiles/",
                "!profiles/codex/",
                "!profiles/codex/**",
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

    def test_containerignore_includes_every_tracked_copy_input(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        tracked = subprocess.run(
            ("git", "ls-files", "--", "Containerfile", "src", "profiles/codex"),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

        self.assertTrue(tracked)
        for path in tracked:
            with self.subTest(path=path):
                self.assertTrue(containerignore_includes(path, patterns))
