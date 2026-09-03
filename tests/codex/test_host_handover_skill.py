from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HostHandoverSkillTest(unittest.TestCase):
    def test_skill_uses_the_constrained_publisher(self) -> None:
        skill = (
            ROOT / "profiles" / "host-codex" / "skills" / "handover" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertTrue(skill.startswith("---\nname: handover\n"))
        for required in (
            "/home/tsu/.local/libexec/agent-container/agent-handover-host publish",
            "/tmp/agent-handover-",
            "mode `0600`",
            "CODEX_SESSION_ID",
            "git status --short --branch",
            "exactly these seven sections",
            "認証情報",
            "github_pat_",
            "## 次の一手",
        ):
            with self.subTest(required=required):
                self.assertIn(required, skill)

        self.assertNotIn("agent-handover create", skill)
        self.assertNotIn("install -m 600", skill)
        self.assertNotIn("--project", skill)
        self.assertNotIn("--destination", skill)

    def test_claude_skill_uses_the_constrained_publisher(self) -> None:
        skill = (
            ROOT / "profiles" / "host-claude" / "skills" / "handover" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertTrue(skill.startswith("---\nname: handover\n"))
        for required in (
            "allowed-tools:",
            "/home/tsu/.local/libexec/agent-container/agent-handover-host publish",
            "/tmp/agent-handover-",
            "mode `0600`",
            "CLAUDE_SESSION_ID",
            "git status --short --branch",
            "exactly these seven sections",
            "認証情報",
            "github_pat_",
            "## 次の一手",
        ):
            with self.subTest(required=required):
                self.assertIn(required, skill)

        self.assertNotIn("agent-handover create", skill)
        self.assertNotIn("install -m 600", skill)
        self.assertNotIn("--project", skill)
        self.assertNotIn("--destination", skill)


if __name__ == "__main__":
    unittest.main()
