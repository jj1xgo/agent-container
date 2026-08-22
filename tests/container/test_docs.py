from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class Phase1DocumentationTest(unittest.TestCase):
    def test_operator_guide_contains_complete_safe_flow(self) -> None:
        body = (ROOT / "docs/phase1-codex-container.md").read_text(encoding="utf-8")
        for command in (
            "agentctl build",
            "agentctl auth codex",
            "agentctl project add",
            "agentctl doctor",
            "agentctl run",
        ):
            self.assertIn(command, body)
        self.assertIn("外向き通信はドメイン制限されていません", body)
        self.assertIn("~/.codex をmountしません", body)
        self.assertIn("device codeによるCodexログイン", body)
        self.assertIn("GitHub CLIの認証（`gh`）は事前に専用状態directoryへ準備", body)
        self.assertIn("認証fileは`0600`、状態directoryは`0700`", body)
        self.assertIn("既存workspaceを上書きしません", body)
        self.assertIn("credential本文", body)
        self.assertIn("表示しません", body)

    def test_smoke_guide_forbids_main_and_secret_output(self) -> None:
        body = (ROOT / "docs/phase1-smoke-test.md").read_text(encoding="utf-8")
        self.assertIn("mainへ直接pushしない", body)
        self.assertIn("credential本文を表示しない", body)
        self.assertIn("codex login status", body)
        self.assertIn("gh auth status", body)
        self.assertIn("別途利用者承認後にだけ行う", body)
        self.assertIn("PRはmergeしない", body)

    def test_smoke_results_start_with_only_not_run_observations(self) -> None:
        body = (ROOT / "docs/phase1-smoke-test.md").read_text(encoding="utf-8")
        table = body.split("| command/check | expected result | observed result | date |", 1)[1]
        rows = [line for line in table.splitlines() if line.startswith("| ")][1:]
        self.assertEqual(len(rows), 14)
        for row in rows:
            columns = [column.strip() for column in row.strip("|").split("|")]
            self.assertEqual(len(columns), 4)
            self.assertEqual(columns[2], "not run")
