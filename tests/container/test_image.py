from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ContainerImageContractTest(unittest.TestCase):
    def test_image_installs_system_bubblewrap(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        install = re.search(
            r"apt-get install -y --no-install-recommends (?P<packages>[^\n]+)",
            body,
        )

        self.assertIsNotNone(install)
        self.assertIn("bubblewrap", install.group("packages").split())

    def test_image_pins_codex_and_runs_as_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("ARG CODEX_VERSION=0.149.0", body)
        self.assertIn("@openai/codex@${CODEX_VERSION}", body)
        self.assertIn("USER agent", body)
        self.assertIn("WORKDIR /workspace", body)
        self.assertIn("COPY src /opt/agent-container/src", body)
        self.assertIn("COPY profiles/codex /opt/agent-container/profiles/codex", body)

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
            ],
        )

        sensitive_nested_paths = (
            "nested/agent-container/shared-auth/codex/auth.json",
            "nested/agent-container/gh/hosts.yml",
            "nested/agent-container/workspaces/project/.git/config",
            "nested/agent-container/projects/project/codex-home/session.json",
            "nested/.worktrees/topic/index",
            "nested/cache/archive.bin",
        )
        allowed_roots = ("src/", "profiles/codex/")
        for path in sensitive_nested_paths:
            with self.subTest(path=path):
                self.assertNotEqual(path, "Containerfile")
                self.assertFalse(path.startswith(allowed_roots))
