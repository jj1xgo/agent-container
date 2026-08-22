from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HandoverSkillTest(unittest.TestCase):
    def test_skill_declares_trigger_and_safety_workflow(self) -> None:
        skill = (
            ROOT / "profiles" / "codex" / "skills" / "handover" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: handover\n"))
        for required in (
            "AGENT_HANDOVER_ROOT",
            "AGENT_PROJECT_ID",
            "agent_container.handover_cli",
            "git status",
            "検証結果",
            "認証情報",
            "github_pat_",
            "次の一手",
        ):
            with self.subTest(required=required):
                self.assertIn(required, skill)


if __name__ == "__main__":
    unittest.main()
