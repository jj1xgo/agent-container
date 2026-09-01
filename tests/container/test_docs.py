from pathlib import Path
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUFF_REQUIREMENTS = "ruff==0.16.4\n"
RUFF_CONFIG = '''target-version = "py311"

[lint]
select = ["E4", "E7", "E9", "F"]
'''
LINT_ENTRYPOINT = '''#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)

exec python3 -m ruff check \\
  --config "$REPO_ROOT/ruff.toml" \\
  "$REPO_ROOT/src" \\
  "$REPO_ROOT/tests"
'''
LINT_INSTALL_STEP = '''      - name: Install lint dependency
        run: |
          python3 -m pip install --disable-pip-version-check --no-deps \\
            -r requirements-lint.txt
'''
LINT_RUN_STEP = '''      - name: Run Ruff lint
        run: bin/lint
'''
README_LINT_SHARED_CONTRACT = (
    "Codex、Claude Code、local developer、CIは同じ`bin/lint` wrapperを使います。"
)


def _logical_containerfile_instructions(body: str) -> tuple[str, ...]:
    instructions = []
    current = ""
    for line in body.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        continued = stripped.endswith("\\")
        part = stripped[:-1].rstrip() if continued else stripped
        current = f"{current} {part}".strip()
        if not continued:
            instructions.append(current)
            current = ""
    if current:
        instructions.append(current)
    return tuple(instructions)


def _has_broad_container_context_copy(body: str) -> bool:
    for instruction in _logical_containerfile_instructions(body):
        match = re.fullmatch(r"(?is)(?:COPY|ADD)\s+(.+)", instruction)
        if match is None:
            continue
        payload = match.group(1).strip()
        while payload.startswith("--"):
            _, separator, payload = payload.partition(" ")
            if not separator:
                payload = ""
                break
            payload = payload.lstrip()
        try:
            if payload.startswith("["):
                values = json.loads(payload)
                sources = values[:-1] if isinstance(values, list) else []
            else:
                values = shlex.split(payload)
                sources = values[:-1]
        except (json.JSONDecodeError, ValueError):
            continue
        if any(source in {".", "./"} for source in sources):
            return True
    return False


def _shell_block(path: Path, needle: str) -> str:
    body = path.read_text(encoding="utf-8")
    return next(
        block
        for block in re.findall(r"```(?:bash|sh)\n(.*?)```", body, re.DOTALL)
        if needle in block
    )


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _create_partial_state(temp_root: Path) -> dict[str, Path]:
    project = temp_root / "agent-container/projects/agent-container-smoke"
    project.mkdir(parents=True, mode=0o700)
    project.chmod(0o700)
    policy = project / "github-broker.json"
    manifest = project / "smoke-fixtures.json"
    for item in (policy, manifest):
        item.write_text("{}", encoding="utf-8")
        item.chmod(0o600)
    handover = temp_root / "handovers/agent-container-smoke"
    handover.mkdir(parents=True, mode=0o700)
    handover.chmod(0o700)
    return {
        "project": project,
        "policy": policy,
        "manifest": manifest,
        "handover": handover,
        "project_json": project / "project.json",
        "workspace": temp_root / "agent-container/workspaces/agent-container-smoke",
    }


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_dir():
        path.rmdir()
    elif path.exists():
        path.unlink()
    path.symlink_to(target)


def _without_filesystem_invariant(
    block: str, target_name: str, mutation: str
) -> str:
    targets = {
        "project": "$AGENT_CONTAINER_HOME/projects/agent-container-smoke",
        "policy": "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json",
        "manifest": "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json",
        "handover": "$AGENT_HANDOVER_ROOT/agent-container-smoke",
        "project_json": "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json",
        "workspace": "$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke",
    }
    target = f'"{targets[target_name]}"'
    candidates = []
    for line in block.splitlines():
        if target not in line:
            continue
        if mutation in {"missing", "directory"} and re.match(
            r"test -[df] ", line
        ):
            candidates.append(line)
        elif mutation in {
            "symlink-file",
            "symlink-directory",
            "dangling-symlink",
        } and line.startswith("test ! -L "):
            candidates.append(line)
        elif mutation in {"file", "present-directory"} and line.startswith(
            "test ! -e "
        ):
            candidates.append(line)
        elif mutation == "mode" and "stat -c" in line and (
            "%a:%u" in line or "'%a'" in line
        ):
            candidates.append(line)
        elif mutation == "owner" and "stat -c" in line and (
            "%a:%u" in line or "'%u'" in line
        ):
            candidates.append(line)
    if len(candidates) != 1:
        raise AssertionError(
            f"expected one {target_name}/{mutation} check, got {candidates!r}"
        )
    return block.replace(f"{candidates[0]}\n", "", 1)
README_LINT_BEHAVIOR_CONTRACT = (
    "`bin/lint`は`src`と`tests`のcheck-onlyで、formatやauto-fixを行わず、"
    "実行中にnetwork accessを必要としません。"
)


class RuffLintToolingTest(unittest.TestCase):
    def test_ruff_version_and_rules_are_exact(self) -> None:
        requirement = (ROOT / "requirements-lint.txt").read_text(encoding="utf-8")
        config = (ROOT / "ruff.toml").read_text(encoding="utf-8")
        self.assertEqual(requirement, RUFF_REQUIREMENTS)
        self.assertEqual(config, RUFF_CONFIG)

    def test_common_lint_entrypoint_is_executable_and_network_free(self) -> None:
        lint = ROOT / "bin/lint"
        metadata = lint.lstat()
        body = lint.read_text(encoding="utf-8")
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertFalse(lint.is_symlink())
        self.assertTrue(metadata.st_mode & stat.S_IXUSR)
        self.assertEqual(body, LINT_ENTRYPOINT)

    def test_ci_installs_and_runs_lint_before_python_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        unit_tests = workflow.split("\n  unit-tests:\n", 1)[1].split(
            "\n  podman-integration:\n", 1
        )[0]
        self.assertEqual(unit_tests.count(LINT_INSTALL_STEP), 1)
        self.assertEqual(unit_tests.count(LINT_RUN_STEP), 1)
        install_index = unit_tests.index(LINT_INSTALL_STEP)
        lint_index = unit_tests.index(LINT_RUN_STEP)
        self.assertLess(install_index, lint_index)
        test_indexes = [
            match.start()
            for match in re.finditer(r"python3 -m unittest\b", unit_tests)
        ]
        self.assertTrue(test_indexes)
        for test_index in test_indexes:
            self.assertLess(lint_index, test_index)

    # Break caught: a maintenance-release PR can build only the ordinary image,
    # leaving its release metadata version unverified in the shipped image.
    def test_ci_verifies_release_candidate_image_for_maintenance_pull_requests(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        step_name = "      - name: Verify release candidate image\n"
        self.assertIn(step_name, workflow)
        start = workflow.index(step_name)
        end = workflow.index("\n      - name:", start + 1)
        release_step = workflow[start:end]

        required = (
            "if: github.event_name == 'pull_request' && github.base_ref == 'release/0.4'",
            "from agent_container.release_metadata import RELEASE_VERSION; print(RELEASE_VERSION)",
            "RELEASE_CANDIDATE_IMAGE: localhost/agent-container:release-candidate",
            '--tag "$RELEASE_CANDIDATE_IMAGE"',
            '--build-arg "AGENT_CONTAINER_VERSION=$release_version"',
            '"agentctl $release_version"',
            "tests.integration.test_project_image_podman",
            "AGENT_CONTAINER_INTEGRATION_BASE_IMAGE: ${{ env.RELEASE_CANDIDATE_IMAGE }}",
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertIn(contract, release_step)
                self.assertEqual(release_step.count(contract), 1)

        self.assertNotIn("0.4.1", release_step)

    def test_production_image_does_not_include_ruff_tooling(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertNotIn("requirements-lint.txt", containerfile)
        self.assertNotIn("ruff", containerfile.lower())
        self.assertFalse(_has_broad_container_context_copy(containerfile))

    def test_broad_context_copy_guard_rejects_shell_and_json_forms(self) -> None:
        for instruction in (
            "COPY . /opt/app",
            "COPY ./ /opt/app",
            "ADD . /opt/app",
            "ADD ./ /opt/app",
            "COPY --chown=agent:agent . /opt/app",
            'COPY [".", "/opt/app"]',
            'COPY ["./", "/opt/app"]',
            'ADD [".", "/opt/app"]',
            'ADD ["./", "/opt/app"]',
            "COPY \\\n  . \\\n  /opt/app",
            "ADD --chown=agent:agent \\\n  ./ \\\n  /opt/app",
            'COPY \\\n  ["./", "/opt/app"]',
        ):
            with self.subTest(instruction=instruction):
                self.assertTrue(
                    _has_broad_container_context_copy(instruction), instruction
                )

    def test_broad_context_copy_guard_allows_current_explicit_sources(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        current_copy_lines = "\n".join(
            line
            for line in containerfile.splitlines()
            if line.startswith(("COPY ", "ADD "))
        )
        self.assertTrue(current_copy_lines)
        self.assertFalse(_has_broad_container_context_copy(current_copy_lines))

    def test_readme_documents_the_shared_lint_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(README_LINT_SHARED_CONTRACT, readme)
        self.assertIn(README_LINT_BEHAVIOR_CONTRACT, readme)

    def test_exact_config_contract_rejects_an_extra_rule_in_memory(self) -> None:
        mutated = RUFF_CONFIG.replace('"F"]', '"F", "W"]')
        with self.assertRaises(AssertionError):
            self.assertEqual(mutated, RUFF_CONFIG)


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
            "agent-github pr create",
            "agent-github pr view",
            "agent-github pr checks",
            "既存branchへのupdateを拒否",
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
    def test_current_broker_commands_omit_obsolete_ruleset_confirmation(
        self,
    ) -> None:
        documents = (
            ROOT / "README.md",
            ROOT / "docs/phase3-github-broker.md",
            ROOT / "docs/phase3-github-broker-smoke-test.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            shell_blocks = re.findall(
                r"```(?:bash|sh)\n(.*?)```", body, flags=re.DOTALL
            )
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(shell_blocks)
                self.assertNotIn(
                    "--confirm-force-push-ruleset", "\n".join(shell_blocks)
                )

    def test_current_policy_schema_omits_legacy_ruleset_marker(self) -> None:
        path = (
            ROOT
            / "docs/superpowers/specs/2026-08-29-project-scoped-github-repository-binding-design.md"
        )
        body = path.read_text(encoding="utf-8")
        schema_section = body.split("## Policy schema and compatibility", 1)[1]
        example = re.search(
            r"```json\n(.*?)```", schema_section, flags=re.DOTALL
        )
        self.assertIsNotNone(example)
        policy = json.loads(example.group(1))
        self.assertEqual(
            set(policy),
            {
                "repository",
                "repository_id",
                "default_branch",
                "protected_branches",
            },
        )
        self.assertNotIn("ruleset_confirmed", policy)

    def test_linked_policy_docs_preserve_three_schema_compatibility(self) -> None:
        design = (
            ROOT
            / "docs/superpowers/specs/2026-08-29-project-scoped-github-repository-binding-design.md"
        ).read_text(encoding="utf-8")
        plan = (
            ROOT
            / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md"
        ).read_text(encoding="utf-8")

        for body in (design, plan):
            self.assertIn("legacy global-binding four-key schema", body)
            self.assertIn("legacy project-bound five-key schema", body)
            self.assertIn("marker-free four-key schema", body)
        self.assertRegex(design, r"removes\s+the legacy marker")
        self.assertIn("Accept only those three exact schemas", plan)
        self.assertRegex(
            plan,
            r"serialize\s+the marker-free four-key bound\s+schema",
        )
        self.assertNotIn("serialize the five-key bound schema", plan)

    def test_phase4_plan_runs_distinct_fast_forward_and_nff_denials(self) -> None:
        plan = (
            ROOT
            / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md"
        ).read_text(encoding="utf-8")
        gate = plan.split(
            "### Task 5: Execute Git and Pull Request broker gates", 1
        )[1]
        fast_forward_commit = gate.index(
            'git commit --allow-empty -m "test: Phase 4 create-only fast-forward denial"'
        )
        fast_forward_push = gate.index(
            'git push origin "$smoke_branch"', fast_forward_commit
        )
        unrelated_commit = gate.index("unrelated_oid=$(", fast_forward_push)
        unrelated_push = gate.index(
            'git push --force origin "$unrelated_oid:refs/heads/$smoke_branch"',
            unrelated_commit,
        )

        self.assertLess(fast_forward_commit, fast_forward_push)
        self.assertLess(fast_forward_push, unrelated_commit)
        self.assertLess(unrelated_commit, unrelated_push)
        self.assertIn("ordinary descendant fast-forward denial", gate)
        self.assertIn("unrelated-history non-fast-forward update", gate)

    def test_create_only_docs_keep_stale_lease_automated_and_nondiagnostic(
        self,
    ) -> None:
        documents = (
            ROOT / "docs/superpowers/specs/2026-08-29-phase4-stabilization-release-design.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
            ROOT / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md",
            ROOT / "docs/superpowers/plans/2026-08-25-phase-3-github-broker.md",
        )
        forbidden = (
            "advertisement後・RPC前のremote更新",
            "paused after receive-pack advertisement",
            "host-admin update advances",
            "Spike deterministic stale-lease synchronization",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("advertisementに存在しないref", body)
                self.assertIn("nonzero old OID", body)
                self.assertIn("tests/container/test_git_protocol.py", body)
                for stale_procedure in forbidden:
                    self.assertNotIn(stale_procedure, body)

        phase4_plan = documents[2].read_text(encoding="utf-8")
        self.assertIn("real-hostでは個別にdiagnosticではない", phase4_plan)
        self.assertIn("`PARTIAL`", phase4_plan)

    def test_phase4_evidence_vocabulary_includes_fail(self) -> None:
        documents = (
            ROOT / "docs/superpowers/specs/2026-08-29-phase4-stabilization-release-design.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
            ROOT / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertRegex(
                    body,
                    r"`PASS`[^\n]+`PARTIAL`[^\n]+`FAIL`[^\n]+`not run`",
                )

    def test_phase3_smoke_uses_current_broker_doctor_classifications(
        self,
    ) -> None:
        smoke = (ROOT / "docs/phase3-github-broker-smoke-test.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "PASS  github-broker: local App and project repository binding valid",
            smoke,
        )
        self.assertIn(
            "PASS  github-broker: local App and legacy global repository binding valid",
            smoke,
        )
        self.assertNotIn("local App and project policy valid", smoke)

    def test_normative_docs_require_create_only_broker_branches(self) -> None:
        documents = (
            ROOT / "README.md",
            ROOT / "docs/phase3-github-broker.md",
            ROOT / "docs/phase3-github-broker-smoke-test.md",
            ROOT / "docs/superpowers/specs/2026-08-25-phase-3-github-broker-design.md",
            ROOT / "docs/superpowers/specs/2026-08-29-project-scoped-github-repository-binding-design.md",
            ROOT / "docs/superpowers/specs/2026-08-29-phase4-stabilization-release-design.md",
            ROOT / "docs/superpowers/plans/2026-08-25-phase-3-github-broker.md",
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("既存branchへのupdateを拒否", body)
                self.assertIn("fast-forward", body)
                self.assertIn("新しいbranch", body)

    def test_phase4_smoke_preserves_failed_force_push_gate_evidence(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "HTTP 403",
            "upgrade-or-public",
            "初回のdisposable branch作成は成功",
            "protected main、delete、tagは拒否",
            "unrelated-history force pushは成功",
            "runtime内の最終OID checkは実行されなかった",
            "別のbounded host observation",
            "remote branchが変更された",
            "retry、復元、PR、Issue、cleanup、releaseは実施していない",
            "receive-pack hang",
            "receive-pack修正",
        ):
            self.assertIn(required, smoke)

    def test_phase4_smoke_records_successful_create_only_rerun(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "test/phase4-create-only-20260829-102228",
            "通常のfast-forward push",
            "unrelated-history force pushを拒否",
            "audit line countが34から35へexactly 1件増え",
            "runtime `8fc3608504080047`",
            "`git-receive-pack`が`denied`",
            "remoteは初回作成commitのまま",
            "local remote-tracking refが存在しなかった",
            "`remote_branch_unchanged=false`は非診断結果",
            "audit line countが37",
            "`git-upload-pack`が`ok`",
            "修正版再検証PASS",
        ):
            self.assertIn(required, smoke)

    def test_phase4_smoke_records_issue_and_stale_client_gates(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "古いimageにはIssue subcommandが存在せず",
            "sandbox内からbroker socketへ接続する前に拒否",
            "audit line countは37のまま",
            "runtime `d704531d03f52b1b`",
            "issue_list_success=true",
            "issue_view_1_success=true",
            "issue_view_2_success=true",
            "open_issue_present=true",
            "closed_issue_excluded_from_list=true",
            "pull_request_excluded_from_list=true",
            "open_issue_fixed_schema_valid=true",
            "closed_issue_fixed_schema_valid=true",
            "open_body_sentinel_present=true",
            "closed_body_sentinel_present=true",
            "excluded_field_sentinel_absent=true",
            "pull_request_sentinel_absent=true",
            "audit line countが37から40へ",
            "Issue data gateの最終再実行はPASS",
            "runtime artifactsは消失",
            "stale clientは拒否",
            "runtime_artifacts_removed=true",
            "stale_client_denied=true",
            "stale_stdout_empty=true",
            "stale_stderr_fixed=true",
            "audit_unchanged=true",
            "stale_temp_removed=true",
            "stdoutは空",
            "`error: GitHub broker request failed`",
            "audit line countは40のまま",
            "exact `$stale_tmp` directoryだけを削除",
            "Cleanup/stale client gateはPASS",
        ):
            self.assertIn(required, smoke)

    def test_v040_release_notes_match_verified_phase4_scope(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Latest stable: `v0.4.1`", readme)
        self.assertIn(
            "## [Unreleased]\n\n## [0.4.1] - 2026-09-01"
            "\n\n### Fixed\n\n"
            "- `v0.4.0`は`v0.3.0`のresolver baseを保持していたため、"
            "公開済みtagはimmutableのまま維持し、`v0.4.1`でexact-tag outputを修正しました。"
            "\n\n## [0.4.0] - 2026-08-29",
            changelog,
        )
        self.assertIn(
            "[0.4.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.4.0",
            changelog,
        )
        for required in (
            "Issue list/view read-only",
            "Git 2.53",
            "create-only",
            "修正版の実host gate",
            "family Issue create/comment",
            "domain allowlist",
        ):
            self.assertIn(required, changelog)
        self.assertRegex(
            smoke,
            r"\| Release gate \| [^\n]+ \| not run \| — \|",
        )

    def test_tracked_operational_docs_do_not_advertise_obsolete_id_lookup(
        self,
    ) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "README.md", "CHANGELOG.md", "docs"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        markdown = tuple(
            ROOT / item.decode("utf-8")
            for item in tracked
            if item.endswith(b".md")
        )
        self.assertTrue(markdown)
        for path in markdown:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("--json databaseId", body)
                self.assertNotIn("--jq .databaseId", body)

    def test_release_candidate_probe_matches_container_image_layout(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        installed_wrappers = set(
            re.findall(
                r"(?m)^COPY --chmod=0755 \S+ /usr/local/bin/(\S+)$",
                containerfile,
            )
        )
        self.assertNotIn("agentctl", installed_wrappers)
        self.assertRegex(
            containerfile,
            r"apt-get install[^\n]*(?:\\\n[^\n]*)*\bpython3\b",
        )
        self.assertIn("COPY src /opt/agent-container/src", containerfile)
        self.assertIn("PYTHONPATH=/opt/agent-container/src", containerfile)
        self.assertTrue((ROOT / "src/agent_container/agentctl.py").is_file())

        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        expected = ("python3", "-m", "agent_container.agentctl", "--version")
        for path in documents:
            body = path.read_text(encoding="utf-8")
            probe = next(
                line.strip().removesuffix(" >/dev/null")
                for line in body.splitlines()
                if line.startswith(
                    "podman run --rm localhost/agent-container:dev "
                )
                and "agentctl" in line
                and "--version" in line
            )
            command = tuple(shlex.split(probe)[4:])
            with self.subTest(path=path.name):
                self.assertEqual(command, expected)

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

    def test_repository_binding_and_observed_failure_are_documented(self) -> None:
        operator = (ROOT / "docs/phase3-github-broker.md").read_text(
            encoding="utf-8"
        )
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for body in (operator, smoke):
            for required in (
                "--github-repository-id",
                "project-scoped",
                "legacy global fallback",
                "upload-discovery",
                "remote App selection",
            ):
                with self.subTest(document=body[:40], required=required):
                    self.assertIn(required, body)
            self.assertIn("global App metadataのrepository IDが", body)
            self.assertIn("smoke repository", body)
        for required_recovery_detail in (
            "mode `0700`のproject directory",
            "mode `0600`の旧schema broker policy",
            "mode `0600` `smoke-fixtures.json`",
            "mode `0700`のhandover directory",
            "`project.json`とworkspaceは作成されていない",
            "fresh approval",
        ):
            self.assertIn(required_recovery_detail, smoke)

    def test_phase4_smoke_records_only_observed_host_recovery_results(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        expected_rows = (
            (
                "Scope reconciliation",
                "initial design, README, and operator guide agree",
                "PASS",
                "2026-08-29",
            ),
            (
                "Fixture repository",
                "private exact repository and fixtures; ruleset inventory",
                "FAIL/PARTIAL: HTTP 403 upgrade-or-public",
                "2026-08-29",
            ),
            (
                "Project registration and local doctor",
                "one approved registration; manifest preserved; smoke Codex/Claude and production doctor",
                "PASS",
                "2026-08-29",
            ),
            (
                "Git/PR gate",
                "new branch succeeds; existing branch/protected/delete/tag updates denied",
                "PASS: 修正前FAILを保持し、修正版再検証PASS。fast-forward／unrelated-history更新を拒否しremote不変",
                "2026-08-29",
            ),
            (
                "Issue data gate",
                "list/view/body fixed schema; Pull Request除外; excluded sentinel absent",
                "PASS: latest image and approved broker socket access; 3 operations `ok`",
                "2026-08-29",
            ),
            (
                "Cleanup/stale client",
                "runtime artifacts removed and stale client denied",
                "PASS: artifacts absent; stale request denied; fixed empty/error output; audit unchanged",
                "2026-08-29",
            ),
        )
        for check, expected, observed, date in expected_rows:
            self.assertIn(
                f"| {check} | {expected} | {observed} | {date} |",
                smoke,
            )
        for check in ("Release gate",):
            self.assertRegex(smoke, rf"\| {check} \| [^\n]+ \| not run \|")
        for required in (
            "upload-discovery",
            "初回登録失敗",
            "一度だけの承認済み登録",
            "fixture manifestのdigestは不変",
            "smoke Codex doctor",
            "smoke Claude doctor",
            "authenticated",
            "production doctor",
            "legacy global repository binding valid",
            "network-policy",
            "private ruleset inventoryはHTTP 403",
            "401／403",
        ):
            self.assertIn(required, smoke)
        self.assertNotRegex(smoke, r"repository ID\s*[=:]\s*\d+")
        self.assertNotIn("base-image-id:", smoke)

    def test_repository_id_lookup_is_non_recording_and_approval_gated(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "README.md",
            ROOT / "docs/phase3-github-broker.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for required in (
                    "set +x",
                    'GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh"',
                    "gh api repos/jj1xgo/agent-container-smoke --jq .id",
                    "host-only",
                    "generic API",
                    "smoke_repository_id=$(",
                    'test "$smoke_repository_id" -gt 0',
                    "smoke_repository_id_valid=true",
                    "# STOP: fresh approval required before registration.",
                    '--github-repository-id "$smoke_repository_id"',
                    "unset smoke_repository_id",
                ):
                    self.assertIn(required, body)
                self.assertNotIn("--json databaseId", body)

                tracing_off = body.index("set +x")
                lookup = body.index("gh api repos/", tracing_off)
                marker = body.index("smoke_repository_id_valid=true", lookup)
                stop = body.index(
                    "# STOP: fresh approval required before registration.", marker
                )
                registration = body.index(
                    '--github-repository-id "$smoke_repository_id"', stop
                )
                unset = body.index("unset smoke_repository_id", registration)
                self.assertLess(tracing_off, lookup)
                self.assertLess(lookup, marker)
                self.assertLess(marker, stop)
                self.assertLess(stop, registration)
                self.assertLess(registration, unset)
                self.assertNotIn("set -x", body[registration:unset])
                self.assertIn("fresh approval", body[stop:registration])

                lookup_block = next(
                    block
                    for block in re.findall(
                        r"```(?:bash|sh)\n(.*?)```", body, flags=re.DOTALL
                    )
                    if "smoke_repository_id=$(" in block
                )
                self.assertIn("smoke_repository_id=$(", lookup_block)
                self.assertLess(
                    lookup_block.index("set +x"),
                    lookup_block.index("smoke_repository_id=$("),
                )
                self.assertLess(
                    lookup_block.index('GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh"'),
                    lookup_block.index("gh api repos/"),
                )
                self.assertNotIn("project add", lookup_block)
                self.assertNotIn('echo "$smoke_repository_id"', lookup_block)
                self.assertNotRegex(
                    lookup_block,
                    r"printf[^\n]*\$smoke_repository_id",
                )
                self.assertNotIn("gh repo view --json id", lookup_block)
                registration_block = next(
                    block
                    for block in re.findall(
                        r"```(?:bash|sh)\n(.*?)```", body, flags=re.DOTALL
                    )
                    if '--github-repository-id "$smoke_repository_id"' in block
                )
                self.assertNotEqual(lookup_block, registration_block)
                self.assertNotIn("gh api repos/", registration_block)

    def test_repository_id_lookup_fails_closed_before_success_marker(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "README.md",
            ROOT / "docs/phase3-github-broker.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        with TemporaryDirectory() as temp:
            temp_root = Path(temp)
            fake_bin = temp_root / "bin"
            _write_executable(
                fake_bin / "gh",
                "#!/bin/sh\n"
                "[ \"$*\" = 'api repos/jj1xgo/agent-container-smoke --jq .id' ] "
                "|| exit 64\n"
                "if [ \"${FAKE_NO_OUTPUT:-0}\" = 0 ]; then\n"
                "  printf '%s\\n' \"${FAKE_REPOSITORY_ID:-123}\"\n"
                "fi\n"
                "[ \"${FAKE_GH_FAIL:-0}\" = 0 ]\n",
            )
            base_environment = {
                **os.environ,
                "AGENT_CONTAINER_HOME": str(temp_root / "agent-container"),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            for path in documents:
                block = _shell_block(path, "smoke_repository_id=$(")
                for value in ("0", "not-decimal", "R_kgDOExample"):
                    with self.subTest(path=path.name, repository_id=value):
                        result = subprocess.run(
                            ["/bin/sh", "-c", block],
                            cwd=ROOT,
                            env={
                                **base_environment,
                                "FAKE_REPOSITORY_ID": value,
                            },
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn(
                            "smoke_repository_id_valid=true", result.stdout
                        )
                with self.subTest(path=path.name, repository_id="no output"):
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=ROOT,
                        env={**base_environment, "FAKE_NO_OUTPUT": "1"},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(
                        "smoke_repository_id_valid=true", result.stdout
                    )
                with self.subTest(path=path.name, repository_id="gh failure"):
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=ROOT,
                        env={**base_environment, "FAKE_GH_FAIL": "1"},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(
                        "smoke_repository_id_valid=true", result.stdout
                    )
                with self.subTest(path=path.name, repository_id="123"):
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=ROOT,
                        env=base_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout, "smoke_repository_id_valid=true\n"
                    )

    def test_recovery_inventory_uses_reviewed_code_before_approval(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        for path in documents:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for required in (
                    "bin/agentctl build >/dev/null",
                    "python3 -m agent_container.agentctl --version >/dev/null",
                    "agent-github --help >/dev/null",
                    "reviewed_candidate_valid=true",
                    "partial_state_filesystem_valid=true",
                    "load_broker_policy",
                    "policy.repository_id is not None",
                    "legacy_policy_valid=true",
                    "fixture_manifest_valid=true",
                    '"repository": "jj1xgo/agent-container-smoke"',
                    '"default_branch": "main"',
                    '"open_issue"',
                    '"closed_issue"',
                    '"pull_request"',
                    "phase4-open-body-sentinel",
                    "phase4-closed-body-sentinel",
                    "phase4-excluded-field-sentinel",
                    "phase4-pr-exclusion-sentinel",
                ):
                    self.assertIn(required, body)
                build = body.index("bin/agentctl build")
                candidate = body.index("reviewed_candidate_valid=true", build)
                filesystem = body.index(
                    "partial_state_filesystem_valid=true", candidate
                )
                policy = body.index("load_broker_policy", build)
                manifest = body.index("fixture_manifest_valid=true", policy)
                lookup = body.index("smoke_repository_id=$(", manifest)
                stop = body.index(
                    "# STOP: fresh approval required before registration.", lookup
                )
                registration = body.index(
                    '--github-repository-id "$smoke_repository_id"', stop
                )
                self.assertLess(build, candidate)
                self.assertLess(candidate, filesystem)
                self.assertLess(filesystem, policy)
                self.assertLess(policy, manifest)
                self.assertLess(manifest, lookup)
                self.assertLess(lookup, stop)
                self.assertLess(stop, registration)
                self.assertNotIn("print(payload)", body[policy:stop])
                self.assertNotIn("print(policy)", body[policy:stop])
                self.assertNotRegex(body[policy:stop], r"(?m)^\s*cat\s+")
                validation_block = next(
                    block
                    for block in re.findall(
                        r"```bash\n(.*?)```", body, flags=re.DOTALL
                    )
                    if "load_broker_policy" in block
                )
                self.assertEqual(
                    re.findall(r'print\("([a-z_]+=true)"\)', validation_block),
                    ["legacy_policy_valid=true", "fixture_manifest_valid=true"],
                )

    def test_reviewed_candidate_block_fails_closed_before_success_marker(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        with TemporaryDirectory() as temp:
            temp_root = Path(temp)
            fake_bin = temp_root / "fake-bin"
            _write_executable(
                temp_root / "bin/agentctl",
                "#!/bin/sh\n[ \"${FAKE_FAIL:-}\" != build ]\n",
            )
            _write_executable(
                fake_bin / "podman",
                "#!/bin/sh\n"
                "case \"$*:${FAKE_FAIL:-}\" in\n"
                "  *'python3 -m agent_container.agentctl --version:version'*) exit 23 ;;\n"
                "  *'agent-github --help:help'*) exit 24 ;;\n"
                "esac\n",
            )
            base_environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            for path in documents:
                block = _shell_block(path, "reviewed_candidate_valid=true")
                for failure in ("build", "version", "help"):
                    with self.subTest(path=path.name, failure=failure):
                        result = subprocess.run(
                            ["/bin/sh", "-c", block],
                            cwd=temp_root,
                            env={**base_environment, "FAKE_FAIL": failure},
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn(
                            "reviewed_candidate_valid=true", result.stdout
                        )
                with self.subTest(path=path.name, failure="none"):
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=temp_root,
                        env=base_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout, "reviewed_candidate_valid=true\n"
                    )

    def test_partial_state_filesystem_block_fails_closed_before_marker(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        invalidations = (
            ("project missing", "project", "missing"),
            ("project symlink", "project", "symlink-directory"),
            ("project mode", "project", "mode"),
            ("project owner", "project", "owner"),
            ("policy not file", "policy", "directory"),
            ("policy symlink", "policy", "symlink-file"),
            ("policy mode", "policy", "mode"),
            ("policy owner", "policy", "owner"),
            ("manifest not file", "manifest", "directory"),
            ("manifest symlink", "manifest", "symlink-file"),
            ("manifest mode", "manifest", "mode"),
            ("manifest owner", "manifest", "owner"),
            ("handover missing", "handover", "missing"),
            ("handover symlink", "handover", "symlink-directory"),
            ("handover mode", "handover", "mode"),
            ("handover owner", "handover", "owner"),
            ("project metadata exists", "project_json", "file"),
            ("project metadata symlink", "project_json", "dangling-symlink"),
            ("workspace exists", "workspace", "present-directory"),
            ("workspace symlink", "workspace", "dangling-symlink"),
        )
        for path in documents:
            block = _shell_block(path, "partial_state_filesystem_valid=true")
            for label, target_name, mutation in invalidations:
                with self.subTest(path=path.name, invalidation=label):
                    with TemporaryDirectory() as temp:
                        temp_root = Path(temp)
                        paths = _create_partial_state(temp_root)
                        target = paths[target_name]
                        fake_bin = temp_root / "fake-bin"
                        real_stat = shutil.which("stat", path=os.environ["PATH"])
                        self.assertIsNotNone(real_stat)
                        _write_executable(
                            fake_bin / "stat",
                            "#!/bin/sh\n"
                            "last=\n"
                            "for argument do last=$argument; done\n"
                            "if [ \"$last\" = \"${FAKE_STAT_TARGET:-}\" ]; then\n"
                            "  case \"${FAKE_STAT_FAILURE:-}:$*\" in\n"
                            f"    follow:*) exec {shlex.quote(real_stat)} -L \"$@\" ;;\n"
                            "    mode:*%a:%u*) printf '%s:%s\\n' \"$FAKE_BAD_MODE\" \"$REAL_UID\"; exit 0 ;;\n"
                            "    owner:*%a:%u*) printf '%s:%s\\n' \"$FAKE_GOOD_MODE\" \"$FAKE_BAD_UID\"; exit 0 ;;\n"
                            "    mode:*%a*) printf '%s\\n' \"$FAKE_BAD_MODE\"; exit 0 ;;\n"
                            "    owner:*%u*) printf '%s\\n' \"$FAKE_BAD_UID\"; exit 0 ;;\n"
                            "  esac\n"
                            "fi\n"
                            f"exec {shlex.quote(real_stat)} \"$@\"\n",
                        )
                        environment = {
                            **os.environ,
                            "HOME": str(temp_root),
                            "AGENT_CONTAINER_HOME": str(
                                temp_root / "agent-container"
                            ),
                            "AGENT_HANDOVER_ROOT": str(temp_root / "handovers"),
                            "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        }
                        if mutation in {"mode", "owner"}:
                            good_mode = "600" if target_name in {
                                "policy",
                                "manifest",
                            } else "700"
                            environment.update(
                                {
                                    "FAKE_STAT_TARGET": str(target),
                                    "FAKE_STAT_FAILURE": mutation,
                                    "FAKE_GOOD_MODE": good_mode,
                                    "FAKE_BAD_MODE": (
                                        "644" if good_mode == "600" else "755"
                                    ),
                                    "REAL_UID": str(os.getuid()),
                                    "FAKE_BAD_UID": str(os.getuid() + 1),
                                }
                            )
                        elif mutation == "missing":
                            environment["FAKE_MISSING_TARGET"] = str(target)
                        elif mutation == "directory":
                            if target.exists():
                                target.unlink()
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.mkdir()
                            target.chmod(0o600)
                        elif mutation == "file":
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text("present", encoding="utf-8")
                        elif mutation == "symlink-file":
                            linked = temp_root / f"linked-{target.name}"
                            linked.write_text("{}", encoding="utf-8")
                            linked.chmod(0o600)
                            _replace_with_symlink(target, linked)
                            environment.update(
                                {
                                    "FAKE_STAT_TARGET": str(target),
                                    "FAKE_STAT_FAILURE": "follow",
                                }
                            )
                        elif mutation == "symlink-directory":
                            if target_name == "project":
                                parked = temp_root / "parked-project"
                                target.rename(parked)
                                target.symlink_to(parked)
                            else:
                                linked = temp_root / f"linked-{target.name}"
                                linked.mkdir(mode=0o700)
                                linked.chmod(0o700)
                                _replace_with_symlink(target, linked)
                            environment.update(
                                {
                                    "FAKE_STAT_TARGET": str(target),
                                    "FAKE_STAT_FAILURE": "follow",
                                }
                            )
                        elif mutation == "present-directory":
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.mkdir()
                        elif mutation == "dangling-symlink":
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.symlink_to(temp_root / "absent-target")
                        executed_block = block
                        if mutation == "missing":
                            executed_block = (
                                "test() {\n"
                                "  if [ \"$#\" -eq 2 ] && [ \"$1\" = -d ] "
                                "&& [ \"$2\" = \"$FAKE_MISSING_TARGET\" ]; then\n"
                                "    return 1\n"
                                "  fi\n"
                                "  command test \"$@\"\n"
                                "}\n"
                                f"{block}"
                            )
                        result = subprocess.run(
                            ["/bin/sh", "-c", executed_block],
                            cwd=ROOT,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn(
                            "partial_state_filesystem_valid=true",
                            result.stdout,
                        )
                        result = subprocess.run(
                            [
                                "/bin/sh",
                                "-c",
                                _without_filesystem_invariant(
                                    executed_block, target_name, mutation
                                ),
                            ],
                            cwd=ROOT,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            result.stdout,
                            "partial_state_filesystem_valid=true\n",
                        )

            with self.subTest(path=path.name, invalidation="none"):
                with TemporaryDirectory() as temp:
                    temp_root = Path(temp)
                    _create_partial_state(temp_root)
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=ROOT,
                        env={
                            **os.environ,
                            "HOME": str(temp_root),
                            "AGENT_CONTAINER_HOME": str(
                                temp_root / "agent-container"
                            ),
                            "AGENT_HANDOVER_ROOT": str(temp_root / "handovers"),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout,
                        "partial_state_filesystem_valid=true\n",
                    )

    def test_fixture_validation_requires_observed_issue_and_pr_numbers(self) -> None:
        documents = (
            ROOT / "docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md",
            ROOT / "docs/phase4-stabilization-smoke-test.md",
        )
        fixture = {
            "repository": "jj1xgo/agent-container-smoke",
            "default_branch": "main",
            "open_issue": 1,
            "closed_issue": 2,
            "pull_request": 3,
            "open_body_sentinel": "phase4-open-body-sentinel",
            "closed_body_sentinel": "phase4-closed-body-sentinel",
            "excluded_field_sentinel": "phase4-excluded-field-sentinel",
            "pull_request_sentinel": "phase4-pr-exclusion-sentinel",
        }
        legacy_policy = {
            "repository": "jj1xgo/agent-container-smoke",
            "default_branch": "main",
            "protected_branches": ["main"],
            "ruleset_confirmed": True,
        }
        for path in documents:
            with self.subTest(path=path.name):
                with TemporaryDirectory() as temp:
                    temp_root = Path(temp)
                    project = (
                        temp_root / "projects/agent-container-smoke"
                    )
                    project.mkdir(parents=True, mode=0o700)
                    project.chmod(0o700)
                    policy_path = project / "github-broker.json"
                    policy_path.write_text(
                        json.dumps(legacy_policy), encoding="utf-8"
                    )
                    policy_path.chmod(0o600)
                    manifest_path = project / "smoke-fixtures.json"
                    block = _shell_block(path, "load_broker_policy")
                    environment = {
                        **os.environ,
                        "AGENT_CONTAINER_HOME": str(temp_root),
                    }

                    for field, wrong_value in (
                        ("open_issue", 4),
                        ("closed_issue", 5),
                        ("pull_request", 6),
                    ):
                        with self.subTest(path=path.name, field=field):
                            manifest_path.write_text(
                                json.dumps({**fixture, field: wrong_value}),
                                encoding="utf-8",
                            )
                            manifest_path.chmod(0o600)
                            result = subprocess.run(
                                ["/bin/sh", "-c", block],
                                cwd=ROOT,
                                env=environment,
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            self.assertNotEqual(result.returncode, 0)
                            self.assertNotIn(
                                "fixture_manifest_valid=true", result.stdout
                            )

                    manifest_path.write_text(
                        json.dumps(fixture), encoding="utf-8"
                    )
                    manifest_path.chmod(0o600)
                    result = subprocess.run(
                        ["/bin/sh", "-c", block],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout,
                        "legacy_policy_valid=true\n"
                        "fixture_manifest_valid=true\n",
                    )

    def test_shared_app_selection_and_production_doctor_are_required(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        operator = (ROOT / "docs/phase3-github-broker.md").read_text(
            encoding="utf-8"
        )
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for body in (readme, operator, smoke):
            self.assertIn("production and smoke selected repositories", body)
            self.assertIn("Do not deselect the production repository", body)
            self.assertIn(
                "each installation token narrows to exactly one project repository ID",
                body,
            )
        self.assertNotIn("このexact repositoryだけを選択", smoke)
        for body in (operator, smoke):
            for command in (
                "bin/agentctl doctor agent-container-smoke --github-broker",
                "bin/agentctl doctor agent-container-smoke --agent claude --github-broker",
                "bin/agentctl doctor agent-container --github-broker",
            ):
                self.assertIn(command, body)


class ReleaseVersionContractTest(unittest.TestCase):
    def test_release_metadata_matches_public_docs(self) -> None:
        from agent_container.release_metadata import (
            DEVELOPMENT_VERSION,
            RELEASE_TAG,
            RELEASE_VERSION,
        )

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"Latest stable: `{RELEASE_TAG}`", readme)
        self.assertIn(f"Development line: `{DEVELOPMENT_VERSION}`", readme)
        self.assertIn(f"## [{RELEASE_VERSION}] - 2026-09-01", changelog)

    def test_ci_records_resolved_version_in_job_summary(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("$GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("bin/agentctl --version", workflow)
