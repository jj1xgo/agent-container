from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class Phase1DocumentationTest(unittest.TestCase):
    def test_readme_starts_with_one_command_setup_flow(self) -> None:
        body = (ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = body.index("## 最短で使う")
        requirements = body.index("## 必要なもの")

        self.assertLess(quickstart, requirements)
        self.assertIn("bin/setup.sh OWNER/REPOSITORY", body)
        self.assertIn("bin/agentctl run REPOSITORY", body)
        self.assertIn("## 手動でセットアップする", body)

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

    def test_operator_guide_documents_claude_handover_broker_contract(self) -> None:
        body = (ROOT / "docs/phase2-claude-code.md").read_text(encoding="utf-8")

        for expected in (
            "agent-handover create --title",
            "`/handovers/PROJECT` | read-only",
            "brokerは`create`だけ",
            "direct writeへfallbackしません",
            "本文、title、capabilityはauditしません",
            "他projectのhandoverはmountにもbrokerにも現れません",
            "chmod 600",
            "mktemp ./handover-body.XXXXXX",
            '< "$handover_body"',
            "## 作業の目的",
            "## 現在地",
            "## 決定事項と理由",
            "## 変更したファイル・commit・PR",
            "## 検証結果",
            "## 未解決事項とリスク",
            "## 次の一手",
            "認証済みClaudeのhandover実host smokeは2026-08-27にPASS",
        ):
            self.assertIn(expected, body)
        self.assertNotIn(": > handover-body.md", body)

    def test_handover_smoke_guide_records_authenticated_checks(self) -> None:
        body = (ROOT / "docs/phase2-smoke-test.md").read_text(encoding="utf-8")

        for expected in (
            "agent-handover create --title",
            "handover project mountはread-only",
            "brokerは`create`だけ",
            "direct writeへfallbackしません",
            "本文、title、capabilityはauditしません",
            "他projectのhandoverは利用できません",
            "認証済みClaudeのhandover実host smokeは2026-08-27にPASS",
        ):
            self.assertIn(expected, body)

        rows = {
            columns[0]: columns
            for line in body.splitlines()
            if line.startswith("| Claude handover ")
            for columns in ([column.strip() for column in line.strip("|").split("|")],)
        }
        self.assertEqual(
            set(rows),
            {
                "Claude handover create",
                "Claude handover direct mutation denial",
                "Claude handover cross-project denial",
                "Claude handover secret rejection",
                "Claude handover non-logging",
                "Claude handover expired capability",
            },
        )
        expected_results = {
            "Claude handover create": "exit 0; path-only stdout; host regular file mode 0600 owner 1000:1000; canonical metadata and seven sections",
            "Claude handover direct mutation denial": "direct create, overwrite, rename, and delete denied read-only; existing file present and content hash unchanged",
            "Claude handover cross-project denial": "other mount absent; overridden project request denied; stdout empty; fixed stderr; audit authentication",
            "Claude handover secret rejection": "malformed sections and dummy credential marker denied; stdout empty; fixed stderr; audit content-policy; no temporary or new entry",
            "Claude handover non-logging": "client output omitted sentinels; audit fixed metadata only and omitted body, title, capability, and credential marker",
            "Claude handover expired capability": "runtime directory, socket, and capability removed; stale client denied; stdout empty; fixed stderr; audit unchanged",
        }
        for name, columns in rows.items():
            self.assertEqual(len(columns), 4)
            self.assertEqual(columns[2], expected_results[name])
            self.assertEqual(columns[3], "2026-08-27")

    def test_codex_retains_direct_handover_path_in_phase_1(self) -> None:
        body = (ROOT / "docs/codex-operations.md").read_text(encoding="utf-8")

        self.assertIn("Phase 1ではCodexは既存のdirect handover pathを維持します", body)


class Phase3DocumentationTest(unittest.TestCase):
    def test_resource_monitor_and_cross_agent_review_are_documented(self) -> None:
        guide = (ROOT / "docs/phase3-resource-review.md").read_text(
            encoding="utf-8"
        )
        for expected in (
            "agentctl stats PROJECT",
            "CPU",
            "MEMORY",
            "PIDS",
            "UPTIME",
            "環境変数",
            "cgroups v2",
            "labelは認証された所有証明ではなく",
            "AI reviewだけで正しいと断定しません",
        ):
            self.assertIn(expected, guide)

        template = (ROOT / ".github/pull_request_template.md").read_text(
            encoding="utf-8"
        )
        for expected in (
            "## Security boundary",
            "## Agent review",
            "Implementation agent",
            "Review agent",
            "## Verification",
            "## Host gates and residual risk",
        ):
            self.assertIn(expected, template)

    def test_operator_guide_documents_broker_setup_and_fixed_operations(self) -> None:
        body = (ROOT / "docs/phase3-github-broker.md").read_text(encoding="utf-8")
        for expected in (
            "Only select repositories",
            "Contents | write",
            "Pull requests | write",
            "Checks | read",
            "app.json",
            "private-key.pem",
            "--confirm-force-push-ruleset",
            "agent-github pr create",
            "agent-github pr view",
            "agent-github pr checks",
            "legacy `gh` credentialへfallbackしません",
        ):
            self.assertIn(expected, body)

    def test_operator_guide_distinguishes_local_doctor_from_host_gate(self) -> None:
        body = (ROOT / "docs/phase3-github-broker.md").read_text(encoding="utf-8")
        self.assertIn("local stateだけの判定", body)
        self.assertIn("実GitHub App", body)
        self.assertIn("利用者承認後にだけ実行", body)
        self.assertIn("外向きnetworkはdomain allowlistされていません", body)

    def test_smoke_guide_covers_negative_and_secret_free_gates(self) -> None:
        body = (ROOT / "docs/phase3-github-broker-smoke-test.md").read_text(encoding="utf-8")
        for expected in (
            "credential本文を表示しない",
            "/proc/*/environ",
            "mainへ直接pushしない",
            "non-fast-forward",
            "agent-github pr merge",
            "generic API",
            "smoke PRはmergeしません",
            "broker停止後",
            "PARTIAL",
            "実host smoke",
        ):
            self.assertIn(expected, body)

    def test_issue_read_only_operator_and_host_smoke_contracts(self) -> None:
        guide = (ROOT / "docs/phase3-github-broker.md").read_text(encoding="utf-8")
        for expected in (
            "agent-github issue list",
            "agent-github issue view NUMBER",
            "Issues | read",
            "最大30件",
            "comment",
            "issue-request",
            "issue_number",
        ):
            self.assertIn(expected, guide)

        smoke = (ROOT / "docs/phase3-github-broker-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for expected in (
            "agent-github issue list",
            "agent-github issue view ISSUE_NUMBER",
            "Pull Requestを除外",
            "credential非露出",
            "expired capability",
            "Git/PR regression",
            "not run",
            "allowlist済みのIssue bodyはstdoutに含まれてよい",
            "excluded raw-response field sentinel",
        ):
            self.assertIn(expected, smoke)
        self.assertNotIn(
            "stdout、stderr、auditがtoken、capability、raw response、fixture sentinelを含まない",
            smoke,
        )
        for check, status in (
            ("Issue App permission", "PARTIAL:"),
            ("Issue list/PR exclusion", "PARTIAL:"),
            ("Issue view/body", "not run"),
            ("Issue write/query/cross-repository denial", "PASS:"),
            ("Issue credential non-exposure", "PARTIAL:"),
            ("Issue expired capability", "PARTIAL:"),
            ("Issue Git/PR regression", "PARTIAL:"),
        ):
            self.assertRegex(smoke, rf"\| {check} \| [^\n]+ \| {status}")
        self.assertIn("PR #53–#55", smoke)
        self.assertIn("2026-08-29", smoke)

    def test_base_image_does_not_leak_legacy_gh_environment_into_broker_mode(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertNotIn("GH_CONFIG_DIR=", containerfile)


class Phase4DocumentationTest(unittest.TestCase):
    def test_phase4_scope_and_release_contract(self) -> None:
        initial = (
            ROOT / "docs/superpowers/specs/2026-08-22-agent-container-design.md"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        operator = (ROOT / "docs/phase3-github-broker.md").read_text(
            encoding="utf-8"
        )
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for body in (initial, readme, operator):
            self.assertIn("family Issue create/comment", body)
            self.assertIn("将来Phase", body)
            self.assertIn("domain allowlist", body)
        for required in (
            "jj1xgo/agent-container-smoke",
            "private repository",
            "stale client",
            "Pull Request除外",
            "non-fast-forward",
            "v0.4.0",
            "最終承認",
        ):
            self.assertIn(required, smoke)

    def test_phase4_smoke_starts_without_claiming_results(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for check in (
            "Scope reconciliation",
            "Fixture repository",
            "Git/PR gate",
            "Issue data gate",
            "Cleanup/stale client",
            "Release gate",
        ):
            self.assertRegex(smoke, rf"\| {check} \| [^\n]+ \| not run \|")
