# Codex Operations Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codexのcontext・利用量を確認できる標準statuslineと、別セッションへ安全に作業を渡す明示的handover、起動時の発見hookを試作する。

**Architecture:** `profiles/codex/`にはコンテナへ配布するCodex設定とSkillを置き、`src/agent_container/`にはhandoverのpath生成・template生成・SessionStart hookを置く。handover本文は外部のproject別directoryへ保存し、hookは本文を自動注入せず最新ファイルのpathだけを追加contextとして通知する。

**Tech Stack:** Python 3.11+標準ライブラリ、Codex CLI 0.149.0、TOML、JSON、`unittest`

**Spec:** `docs/superpowers/specs/2026-08-22-agent-container-design.md`

## Global Constraints

- Linuxホストとrootless Podmanだけを初期対象とする。
- ホストの実`~/.codex`、`~/.claude`、開発workspaceをread-writeマウントしない。
- 初期段階では設定を実`~/.codex`へinstallせず、repository内の配布元とtestだけを作る。
- handover保存先は`handovers/<project>/YYYY-MM-DD_HHMM.md`とする。
- Obsidian vault全体をコンテナへmountしない。
- hookはhandover本文を自動でmodel contextへ入れない。
- credentialの値をログ、prompt、handover、test fixtureへ含めない。
- statuslineはCodex 0.149.0で確認済みのcanonical identifierだけを使う。
- 新しい外部Python dependencyを追加しない。
- Skill実装時は`skill-creator`と`superpowers:writing-skills`を使用する。
- 現在の`/home/tsu/codexpj`はGit repositoryではないため、この計画は新しい`agent-container` repositoryまたはそのworktreeへ移してから実行する。

---

## File Structure

- `profiles/codex/config.toml`: Codex共通設定の配布元。初期statusline順序だけを保持する。
- `profiles/codex/hooks.json`: `SessionStart` hookの配布元。
- `profiles/codex/skills/handover/SKILL.md`: Codexがhandoverを作成・検査する手順。
- `src/agent_container/__init__.py`: Python package marker。
- `src/agent_container/handover.py`: project ID検証、最新handover検索、template作成。
- `src/agent_container/handover_cli.py`: handover templateを作るCLI entry point。
- `src/agent_container/handover_hook.py`: Codex `SessionStart` JSONを処理するhook entry point。
- `tests/codex/test_statusline.py`: statusline設定の順序と値を検証する。
- `tests/__init__.py`: test package marker。
- `tests/codex/__init__.py`: Codex test package marker。
- `tests/codex/test_handover.py`: path、検索、template、安全性を検証する。
- `tests/codex/test_handover_hook.py`: hookのstdin/stdout契約を検証する。
- `tests/codex/test_handover_skill.py`: Skillのfrontmatterと必須手順を検証する。
- `docs/codex-operations.md`: 日常運用と手動smoke testを説明する日本語文書。

### Task 1: Codex statusline profile

**Files:**
- Create: `profiles/codex/config.toml`
- Create: `tests/__init__.py`
- Create: `tests/codex/__init__.py`
- Create: `tests/codex/test_statusline.py`

**Interfaces:**
- Consumes: Codex CLI 0.149.0の`tui.status_line: array<string>`設定。
- Produces: 後続のcontainer imageが専用`CODEX_HOME/config.toml`へ配布できるbase profile。

- [ ] **Step 1: Write the failing profile test**

Create empty `tests/__init__.py` and `tests/codex/__init__.py`, then create this test:

```python
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class StatusLineProfileTest(unittest.TestCase):
    def test_statusline_uses_verified_items_in_operational_order(self) -> None:
        config_path = ROOT / "profiles" / "codex" / "config.toml"
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)

        self.assertEqual(
            config["tui"]["status_line"],
            [
                "model-with-reasoning",
                "context-remaining",
                "five-hour-limit",
                "weekly-limit",
                "used-tokens",
                "git-branch",
                "project-name",
            ],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.codex.test_statusline -v`

Expected: FAIL with `FileNotFoundError` for `profiles/codex/config.toml`.

- [ ] **Step 3: Add the minimal Codex profile**

```toml
[tui]
status_line = [
  "model-with-reasoning",
  "context-remaining",
  "five-hour-limit",
  "weekly-limit",
  "used-tokens",
  "git-branch",
  "project-name",
]
```

- [ ] **Step 4: Run the profile test**

Run: `python3 -m unittest tests.codex.test_statusline -v`

Expected: PASS with one test.

- [ ] **Step 5: Validate the identifiers against the installed CLI**

Run from an interactive Codex 0.149.0 session: `/statusline`

Expected: all seven configured items appear as selectable fields; context and limits may be omitted from the rendered footer until API data is available.

- [ ] **Step 6: Commit the independently testable profile**

```bash
git add profiles/codex/config.toml tests/codex/test_statusline.py
git commit -m "feat: add Codex operational statusline"
```

### Task 2: Handover path and template library

**Files:**
- Create: `src/agent_container/__init__.py`
- Create: `src/agent_container/handover.py`
- Create: `src/agent_container/handover_cli.py`
- Create: `tests/codex/test_handover.py`

**Interfaces:**
- Consumes: `root: pathlib.Path`, `project_id: str`, `title: str`, optional `session_id: str`, optional timezone-aware `now: datetime`.
- Produces: `validate_project_id(project_id) -> str`, `latest_handover(root, project_id) -> Path | None`, `create_handover(root, project_id, title, session_id, now=None) -> Path` and module CLI `python3 -m agent_container.handover_cli create ...`.

- [ ] **Step 1: Write failing unit tests for validation and lookup**

```python
from datetime import datetime
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.handover import create_handover
from agent_container.handover import latest_handover
from agent_container.handover import validate_project_id


class HandoverTest(unittest.TestCase):
    def test_validate_project_id_accepts_repository_style_slug(self) -> None:
        self.assertEqual(validate_project_id("agent-container"), "agent-container")

    def test_validate_project_id_rejects_path_traversal(self) -> None:
        for value in ("../secret", "family/project", "", "."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_project_id(value)

    def test_latest_handover_returns_newest_matching_regular_file(self) -> None:
        with TemporaryDirectory() as temp:
            project_dir = Path(temp) / "agent-container"
            project_dir.mkdir()
            older = project_dir / "2026-08-21_1758.md"
            newest = project_dir / "2026-08-22_1815.md"
            older.write_text("older", encoding="utf-8")
            newest.write_text("newest", encoding="utf-8")
            (project_dir / "notes.md").write_text("ignore", encoding="utf-8")

            self.assertEqual(latest_handover(Path(temp), "agent-container"), newest)

    def test_create_handover_uses_expected_path_and_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            path = create_handover(
                root=Path(temp),
                project_id="agent-container",
                title="Codex運用設計",
                session_id="thread-example",
                now=datetime(2026, 8, 22, 18, 15, tzinfo=timezone.utc),
            )

            self.assertEqual(path.name, "2026-08-22_1815.md")
            body = path.read_text(encoding="utf-8")
            self.assertIn("# Handover: Codex運用設計", body)
            self.assertIn("- Project: agent-container", body)
            self.assertIn("- Session: thread-example", body)
            self.assertIn("## 次の一手", body)

    def test_create_handover_never_overwrites_same_minute(self) -> None:
        with TemporaryDirectory() as temp:
            now = datetime(2026, 8, 22, 18, 15, tzinfo=timezone.utc)
            create_handover(Path(temp), "agent-container", "first", "", now)
            with self.assertRaises(FileExistsError):
                create_handover(Path(temp), "agent-container", "second", "", now)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_container'`.

- [ ] **Step 3: Implement the handover library**

```python
from datetime import datetime
from pathlib import Path
import re


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
HANDOVER_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}\.md$")


def validate_project_id(project_id: str) -> str:
    if project_id in {".", ".."} or PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id must be a single safe repository-style slug")
    return project_id


def _clean_line(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"{field} must be one non-empty line")
    return cleaned


def latest_handover(root: Path, project_id: str) -> Path | None:
    project = root.resolve() / validate_project_id(project_id)
    if not project.is_dir():
        return None
    candidates = [
        path
        for path in project.iterdir()
        if HANDOVER_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    ]
    return max(candidates, key=lambda path: path.name, default=None)


def create_handover(
    root: Path,
    project_id: str,
    title: str,
    session_id: str,
    now: datetime | None = None,
) -> Path:
    project_id = validate_project_id(project_id)
    title = _clean_line(title, "title")
    session = _clean_line(session_id, "session_id") if session_id.strip() else "（未記録）"
    timestamp = now or datetime.now().astimezone()
    project = root.resolve() / project_id
    project.mkdir(parents=True, exist_ok=True)
    path = project / timestamp.strftime("%Y-%m-%d_%H%M.md")
    created = timestamp.isoformat(timespec="minutes")
    body = f"""# Handover: {title}

- Project: {project_id}
- Created: {created}
- Session: {session}

## 作業の目的

## 現在地

## 決定事項と理由

## 変更したファイル・commit・PR

## 検証結果

## 未解決事項とリスク

## 次の一手
"""
    with path.open("x", encoding="utf-8") as stream:
        stream.write(body)
    return path
```

Create `src/agent_container/__init__.py` as an empty file.

- [ ] **Step 4: Run the library tests**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover -v`

Expected: PASS with five tests.

- [ ] **Step 5: Write the failing CLI test**

Add to `tests/codex/test_handover.py`:

```python
import os
import subprocess
import sys


class HandoverCliTest(unittest.TestCase):
    def test_create_command_prints_created_path(self) -> None:
        with TemporaryDirectory() as temp:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_container.handover_cli",
                    "create",
                    "--root",
                    temp,
                    "--project",
                    "agent-container",
                    "--title",
                    "運用引き継ぎ",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            created = Path(result.stdout.strip())
            self.assertTrue(created.is_file())
            self.assertEqual(created.parent.name, "agent-container")
```

- [ ] **Step 6: Run the CLI test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover.HandoverCliTest -v`

Expected: FAIL because `agent_container.handover_cli` does not exist.

- [ ] **Step 7: Implement the CLI entry point**

```python
import argparse
from pathlib import Path

from agent_container.handover import create_handover


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agent-handover")
    subcommands = command.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--session-id", default="")
    return command


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "create":
        path = create_handover(
            root=arguments.root,
            project_id=arguments.project,
            title=arguments.title,
            session_id=arguments.session_id,
        )
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 8: Run all handover tests**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover -v`

Expected: PASS with six tests.

- [ ] **Step 9: Commit the independently usable handover creator**

```bash
git add src/agent_container tests/codex/test_handover.py
git commit -m "feat: add safe handover file creation"
```

### Task 3: SessionStart handover discovery hook

**Files:**
- Create: `src/agent_container/handover_hook.py`
- Create: `profiles/codex/hooks.json`
- Create: `tests/codex/test_handover_hook.py`

**Interfaces:**
- Consumes: Codex `SessionStart` JSON on stdin and environment variables `AGENT_HANDOVER_ROOT`, `AGENT_PROJECT_ID`.
- Produces: no output when configuration or handover is absent; otherwise Codex JSON containing `hookSpecificOutput.hookEventName = "SessionStart"` and a short `additionalContext` with the latest file path.

- [ ] **Step 1: Write failing hook behavior tests**

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


def run_hook(environment: dict[str, str], payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(environment)
    merged["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agent_container.handover_hook"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env=merged,
    )


class HandoverHookTest(unittest.TestCase):
    def test_missing_environment_is_silent(self) -> None:
        result = run_hook({}, {"hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(result.stdout, "")

    def test_missing_handover_is_silent(self) -> None:
        with TemporaryDirectory() as temp:
            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "agent-container"},
                {"hook_event_name": "SessionStart", "source": "startup"},
            )
            self.assertEqual(result.stdout, "")

    def test_latest_path_is_announced_without_body(self) -> None:
        with TemporaryDirectory() as temp:
            project = Path(temp) / "agent-container"
            project.mkdir()
            handover = project / "2026-08-22_1815.md"
            secret_marker = "body-must-not-be-in-context"
            handover.write_text(secret_marker, encoding="utf-8")

            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "agent-container"},
                {"hook_event_name": "SessionStart", "source": "resume"},
            )
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn(str(handover), context)
            self.assertNotIn(secret_marker, context)
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_invalid_project_id_is_silent_and_does_not_escape_root(self) -> None:
        with TemporaryDirectory() as temp:
            result = run_hook(
                {"AGENT_HANDOVER_ROOT": temp, "AGENT_PROJECT_ID": "../outside"},
                {"hook_event_name": "SessionStart", "source": "startup"},
            )
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover_hook -v`

Expected: ERROR because `agent_container.handover_hook` does not exist yet.

- [ ] **Step 3: Implement the hook entry point**

```python
import json
import os
import sys
from pathlib import Path

from agent_container.handover import latest_handover


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("hook_event_name") != "SessionStart":
        return 0
    root = os.environ.get("AGENT_HANDOVER_ROOT")
    project_id = os.environ.get("AGENT_PROJECT_ID")
    if not root or not project_id:
        return 0
    try:
        path = latest_handover(Path(root), project_id)
    except ValueError:
        return 0
    if path is None:
        return 0
    context = (
        f"このprojectの最新handoverがあります: {path}\n"
        "別セッションの続きに必要な場合だけ本文を読み、現在のGit状態と照合してください。"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the hook tests**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover_hook -v`

Expected: PASS with four tests.

- [ ] **Step 5: Write the distributable hook configuration**

```json
{
  "description": "Discover project handovers without injecting their body.",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          {
            "type": "command",
            "command": "PYTHONPATH=/opt/agent-container/src /usr/bin/python3 -m agent_container.handover_hook",
            "statusMessage": "Checking project handover",
            "additionalContextLimit": 1000,
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 6: Add a configuration contract test**

Add to `tests/codex/test_handover_hook.py`:

```python
    def test_hooks_json_registers_only_session_start(self) -> None:
        config = json.loads((ROOT / "profiles" / "codex" / "hooks.json").read_text())
        self.assertEqual(list(config["hooks"]), ["SessionStart"])
        group = config["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "startup|resume|compact")
        handler = group["hooks"][0]
        self.assertIn("agent_container.handover_hook", handler["command"])
        self.assertEqual(handler["additionalContextLimit"], 1000)
```

- [ ] **Step 7: Run the hook suite again**

Run: `PYTHONPATH=src python3 -m unittest tests.codex.test_handover_hook -v`

Expected: PASS with five tests.

- [ ] **Step 8: Commit the independently testable discovery hook**

```bash
git add src/agent_container/handover_hook.py profiles/codex/hooks.json tests/codex/test_handover_hook.py
git commit -m "feat: discover latest handover on Codex start"
```

### Task 4: Explicit handover Skill and Japanese operations guide

**Files:**
- Create: `profiles/codex/skills/handover/SKILL.md`
- Create: `tests/codex/test_handover_skill.py`
- Create: `docs/codex-operations.md`

**Interfaces:**
- Consumes: `AGENT_HANDOVER_ROOT`, `AGENT_PROJECT_ID`, optional session ID, Task 2の`agent_container.handover_cli`。
- Produces: 「handoverを作って」「引き継ぎを残して」で起動できるSkillと、`/resume`・`/compact`・handoverを選ぶ運用説明。

- [ ] **Step 1: Invoke the required skill-authoring guidance**

Read and follow both `skill-creator` and `superpowers:writing-skills` before creating `SKILL.md`.

Expected: the implementer applies the current Codex Skill format rather than relying only on this plan’s remembered format.

- [ ] **Step 2: Write the failing Skill contract test**

```python
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python3 -m unittest tests.codex.test_handover_skill -v`

Expected: FAIL with `FileNotFoundError` for the Skill.

- [ ] **Step 4: Create the handover Skill**

```markdown
---
name: handover
description: 現在の作業を別セッションまたは別エージェントへ引き継ぐMarkdownとして保存する。ユーザーがhandover、引き継ぎ、続きの記録を求めたときに使う。同じCodex会話を再開するだけなら、まずresumeで足りるか確認する。
---

# Handover

## 手順

1. `AGENT_HANDOVER_ROOT`と`AGENT_PROJECT_ID`が設定されていることを、生値を出力せず確認する。未設定なら保存先を推測せず、ユーザーへ伝える。
2. `git status --short --branch`、直近commit、現在の計画、実行済みtestを確認する。未実行の検証を成功したものとして書かない。
3. 次のcommandで空のhandoverを作る。session IDが確実に分かる場合だけ`--session-id`を加える。

   ```bash
   PYTHONPATH=/opt/agent-container/src python3 -m agent_container.handover_cli create --root "$AGENT_HANDOVER_ROOT" --project "$AGENT_PROJECT_ID" --title "Codex作業引き継ぎ"
   ```

4. 作成されたファイルの全sectionを、現在確認できる事実で埋める。特に「決定事項と理由」「検証結果」「未解決事項とリスク」「次の一手」を具体的に書く。
5. 認証情報、PAT、API key、cookie、private key、環境変数の値を書かない。少なくとも`ghp_`、`github_pat_`、`sk-`、`BEGIN PRIVATE KEY`に似た文字列がないか確認し、見つけた場合は値を削除して種類と保管場所だけを書く。
6. `git status`とhandover本文を再確認し、commit、PR、test結果、未commit変更の記述が現状と一致することを確認する。
7. ユーザーへ保存先と、次回最初に行う一手を一文で伝える。

## 禁止事項

- transcript全文をhandoverへ貼り付けない。
- 認証情報の値を確認目的で表示しない。
- 推測した成功、commit、PR、test結果を書かない。
- 同じ会話を`/resume`できるだけの場面でhandoverを乱造しない。
```

- [ ] **Step 5: Run the Skill contract test**

Run: `python3 -m unittest tests.codex.test_handover_skill -v`

Expected: PASS with one test.

- [ ] **Step 6: Write the Japanese operations guide**

Create `docs/codex-operations.md` with these exact sections and operational content:

```markdown
# Codex運用ガイド

## 普段の再開

- 同じ会話を続ける: `/rename`で名前を付け、`/resume`または`codex resume`で再開する。
- contextを整理する: `/status`で使用状況を確認し、必要なときだけ`/compact`する。
- 別セッションへ渡す: handover Skillで外部Markdownを作る。

## statusline

配布元は`profiles/codex/config.toml`。model+reasoning、context残量、primary/secondary limit、使用token、Git branch、project名の順に表示する。API情報やGit情報がない項目は表示されない場合がある。対話的な変更は`/statusline`で行う。

## handover保存境界

launcherは対象projectについてだけ`AGENT_PROJECT_ID`と`AGENT_HANDOVER_ROOT`を設定する。Obsidian vault全体をmountしない。handover本文にはcredentialの値を残さない。

## 起動hook

`profiles/codex/hooks.json`を専用`CODEX_HOME`へ配布する。初回または定義変更後は`/hooks`でcommandを確認してtrustする。hookは最新handoverのpathだけを通知し、本文は必要なときにCodexが読む。

## 手動確認

1. test用のhandover rootとproject IDを設定する。
2. `agent_container.handover_cli create`でhandoverを作る。
3. `handover_hook`へ`SessionStart` JSONを渡し、本文ではなくpathだけが返ることを確認する。
4. 専用`CODEX_HOME`でCodexを起動し、`/hooks`でhookをtrustする。
5. `/statusline`で設定項目と順序を確認する。
6. `/status`でcontextとrate limitの表示を確認する。

## 障害時

- hookが動かない: `/hooks`でsource、hash、trust状態を確認する。
- handoverが見つからない: `AGENT_PROJECT_ID`と狭くmountしたhandover directoryの対応を確認する。
- statusline項目が欠ける: 認証方式、APIデータ、Git repository内かどうかを確認する。
- 古いhandoverが出る: filenameが`YYYY-MM-DD_HHMM.md`であることとproject IDを確認する。
```

- [ ] **Step 7: Run the complete prototype test suite**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS with all statusline, handover library, CLI, hook, and Skill tests.

- [ ] **Step 8: Run syntax and data-format checks**

Run: `python3 -m compileall -q src tests`

Expected: exit code 0 with no output.

Run: `python3 -m json.tool profiles/codex/hooks.json >/dev/null`

Expected: exit code 0 with no output.

Run: `python3 -c 'import tomllib; tomllib.load(open("profiles/codex/config.toml", "rb"))'`

Expected: exit code 0 with no output.

- [ ] **Step 9: Commit the Skill and operating guide**

```bash
git add profiles/codex/skills/handover/SKILL.md tests/codex/test_handover_skill.py docs/codex-operations.md
git commit -m "docs: define Codex handover operations"
```

### Task 5: Prototype acceptance verification

**Files:**
- Modify: `docs/codex-operations.md`

**Interfaces:**
- Consumes: Tasks 1–4のcommitted成果物。
- Produces: automated evidenceと、専用test環境で再現可能なmanual verification record。

- [ ] **Step 1: Run all automated verification from a clean shell**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: every discovered test reports `ok` and the command exits 0.

- [ ] **Step 2: Verify the hook never injects handover body**

Run:

```bash
prototype_root="$(mktemp -d)"
mkdir -p "$prototype_root/agent-container"
printf '%s\n' 'DO-NOT-INJECT-BODY' > "$prototype_root/agent-container/2026-08-22_1815.md"
printf '%s\n' '{"hook_event_name":"SessionStart","source":"startup"}' | AGENT_HANDOVER_ROOT="$prototype_root" AGENT_PROJECT_ID="agent-container" PYTHONPATH=src python3 -m agent_container.handover_hook
```

Expected: JSON includes the file path and does not include `DO-NOT-INJECT-BODY`.

- [ ] **Step 3: Verify path traversal is rejected**

Run:

```bash
prototype_root="$(mktemp -d)"
PYTHONPATH=src python3 -m agent_container.handover_cli create --root "$prototype_root" --project "../outside" --title "rejected"
```

Expected: non-zero exit with the safe-slug validation message, and no file is created outside `prototype_root`.

- [ ] **Step 4: Verify Codex parses the dedicated profile without touching the host profile**

Run:

```bash
test_codex_home="$(mktemp -d)"
cp profiles/codex/config.toml "$test_codex_home/config.toml"
cp profiles/codex/hooks.json "$test_codex_home/hooks.json"
CODEX_HOME="$test_codex_home" codex --strict-config --version
```

Expected:

- Codex reports version `0.149.0` and exits 0 without a strict-config error.
- the real host `~/.codex` remains unchanged because `CODEX_HOME` points to the temporary directory.
- authenticated TUI rendering and `/hooks` trust are recorded as Phase 1 integration checks, not claimed by this unauthenticated prototype.

- [ ] **Step 5: Record tested versions and observed conditional fields**

Append a `## 検証記録` section to `docs/codex-operations.md` containing the date, `codex --version`, Python version, test command summary, strict-config result, and the remaining Phase 1 TUI checks. Do not claim that conditional rate-limit fields rendered unless an authenticated dedicated-container smoke test actually confirmed them. Do not include account identifiers, paths containing user secrets, or credential values.

- [ ] **Step 6: Commit the acceptance record**

```bash
git add docs/codex-operations.md
git commit -m "test: record Codex operations smoke test"
```
