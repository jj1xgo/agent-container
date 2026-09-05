# agent-container の作業指示

## プロジェクトの入口

- Linux・rootless Podman 上で Codex / Claude Code を隔離する Python プロジェクト。実装は `src/agent_container/`、配布設定は `profiles/`、CI は `.github/workflows/ci.yml`。
- 必要な範囲で `README.md`、`docs/development-roadmap.md`、対象の仕様・計画を読む。Codex の運用は `docs/codex-operations.md` を参照する。
- `docs/agent-collaboration-notes.md` は未採用・未検証の分担案。確定したルールやモデルの優劣として扱わない。

## 検証

以下はルートで実行する代表例。変更に関係する検証を選び、必須 CI と対象仕様の要求を満たす。

| 対象 | コマンド・確認 |
| --- | --- |
| 文書・指示 | 内容・参照先・既存指示との整合。追跡済み差分は `git diff --check`、stage 後は `git diff --cached --check`。未追跡ファイルは別途確認 |
| Python の静的検査 | `bin/lint`（依存は `requirements-lint.txt`） |
| Codex 関連 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex` |
| container / broker 関連 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container`。局所変更では対象モジュールから確認 |
| socket・実 Podman・実ホスト | `.github/workflows/ci.yml` と対象 smoke test 文書の前提・有効化変数・承認範囲を確認して実行 |

## Code Review Rules

- credential・capability の露出、mount / network / filesystem の権限拡大、broker の制限を迂回する fallback を確認する。変更が必要なら、依頼・設計上の根拠と検証を確認する。
- runtime の変更では停止順序、失敗時の再試行、thread と file descriptor の回収、audit の完了条件を確認する。
- 振る舞い保存が条件のリファクタリングでは wire byte、例外、audit、停止順序の意図しない変更を確認する。既存不具合の修正を混ぜる際は対象計画の制約と依頼の範囲を照合する。
