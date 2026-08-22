from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ContainerImageContractTest(unittest.TestCase):
    def test_image_pins_codex_and_runs_as_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("ARG CODEX_VERSION=0.149.0", body)
        self.assertIn("@openai/codex@${CODEX_VERSION}", body)
        self.assertIn("USER agent", body)
        self.assertIn("WORKDIR /workspace", body)
        self.assertIn("COPY src /opt/agent-container/src", body)
        self.assertIn("COPY profiles/codex /opt/agent-container/profiles/codex", body)

    def test_containerignore_excludes_git_and_local_state(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        for required in (".git", ".worktrees", ".codex", "auth.json", "__pycache__"):
            self.assertIn(required, patterns)
