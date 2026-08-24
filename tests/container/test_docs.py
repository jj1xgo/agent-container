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

    def test_smoke_results_record_all_completed_controller_checks(self) -> None:
        body = (ROOT / "docs/phase1-smoke-test.md").read_text(encoding="utf-8")
        table = body.split("| command/check | expected result | observed result | date |", 1)[1]
        rows = [line for line in table.splitlines() if line.startswith("| ")][1:]
        self.assertEqual(len(rows), 14)
        for row in rows:
            columns = [column.strip() for column in row.strip("|").split("|")]
            self.assertEqual(len(columns), 4)
            self.assertNotIn("not run", columns[2].lower())
            self.assertTrue(columns[2])
            self.assertRegex(columns[2], r"\bexit \d+\b")
            self.assertEqual(columns[3], "2026-08-22")

    def test_shared_auth_row_records_rewrite_without_raw_file_metadata(self) -> None:
        body = (ROOT / "docs/phase1-smoke-test.md").read_text(encoding="utf-8")
        row = next(
            line for line in body.splitlines() if line.startswith("| shared auth update |")
        )
        observed = [column.strip() for column in row.strip("|").split("|")][2]

        self.assertIn("device auth exit 0", observed)
        self.assertIn("mtime changed", observed)
        self.assertIn("inode unchanged", observed)
        self.assertIn("codex login status` exit 0", observed)
        self.assertIn("doctor exit 0", observed)
        self.assertNotIn("mtime unchanged", observed)
        self.assertNotRegex(observed, r"(?:mtime|inode)\s*[=:]\s*[0-9]")


class Phase2DocumentationTest(unittest.TestCase):
    def test_operator_guide_documents_project_derived_images(self) -> None:
        body = (ROOT / "docs/phase2-claude-code.md").read_text(encoding="utf-8")

        for expected in (
            ".agent-container.d/packages.txt",
            ".agent-container.d/node-version.txt",
            ".claude-container.d",
            "run時に自動build",
            "doctorはread-only",
            "runtime中にpackageをinstallしません",
            "findsummits",
            "sotlas-frontend",
        ):
            self.assertIn(expected, body)

    def test_operator_guide_documents_codex_default_and_build_only_updates(self) -> None:
        body = (ROOT / "docs/phase2-claude-code.md").read_text(encoding="utf-8")

        self.assertIn("bin/agentctl doctor PROJECT", body)
        self.assertIn("既定でCodex", body)
        self.assertIn("DISABLE_UPDATES=1", body)
        self.assertIn("version変更はimageの再build時だけ", body)

    def test_operator_guide_contains_claude_contracts(self) -> None:
        body = (ROOT / "docs/phase2-claude-code.md").read_text(encoding="utf-8")
        for command in (
            "agentctl build",
            "agentctl auth claude",
            "agentctl migrate claude",
            "--agent claude",
            "--agent all",
        ):
            self.assertIn(command, body)
        for boundary in (
            "~/.claude",
            ".credentials.json",
            "0700",
            "0600",
            "dry-run",
            "外向き通信はドメイン制限されていません",
        ):
            self.assertIn(boundary, body)
        self.assertIn("credential本文を表示しません", body)
        self.assertIn("旧claude-containerを変更しません", body)

    def test_smoke_guide_contains_required_safety_checks(self) -> None:
        body = (ROOT / "docs/phase2-smoke-test.md").read_text(encoding="utf-8")
        for expected in (
            "利用者承認",
            "mainへ直接pushしない",
            "credential本文を表示しない",
            "claude auth status",
            "認証更新",
            "旧claude-container",
        ):
            self.assertIn(expected, body)

    def test_smoke_guide_contains_claude_sandbox_security_gate(self) -> None:
        body = (ROOT / "docs/phase2-smoke-test.md").read_text(encoding="utf-8")

        for expected in (
            "oauth_token_visible=false",
            "token_file_readable=false",
            "parent_token_via_proc_readable=false",
            "/sandbox",
            "Config",
            "/hooks",
            "/mcp",
            "parent_token_via_proc_readable=true",
            "値",
            "長さ",
            "prefix",
            "hash",
            "環境一覧",
            "/proc/*/environ",
        ):
            self.assertIn(expected, body)

        self.assertIn("即座に停止", body)

    def test_operator_docs_define_final_nested_claude_constraints(self) -> None:
        phase2 = (ROOT / "docs/phase2-claude-code.md").read_text(encoding="utf-8")
        codex = (ROOT / "docs/codex-operations.md").read_text(encoding="utf-8")

        for expected in (
            "global scrubは意図的に設定しません",
            "強いsandboxを強制",
            "hooksとMCPは初期状態で無効",
            "review済みHTTP MCP",
            "stdio MCP",
            "parent_token_via_proc_readable=true",
            "運用を停止",
            "--read-only",
            "--cap-drop=all",
            "no-new-privileges",
        ):
            self.assertIn(expected, phase2)

        self.assertIn("Claudeのmanaged sandbox", codex)
        self.assertIn("Codexのhook設定とは別", codex)
