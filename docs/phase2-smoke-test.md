# Phase 2 Claude Code 実host smoke test

このchecklistは、networked build、Claude対話認証、Podman host、Git操作を伴う実host確認です。unit testの代替ではありません。各外部操作は実行直前に利用者承認を得ます。現時点では全観測が`not run`です。

## 共通の安全規則

- credential本文を表示しない。token、browser code、`.credentials.json`本文、認証設定本文、環境変数値を記録しない。
- `mainへ直接pushしない`。merge、force-push、release、repository削除は行わない。
- hostの`~/.claude`、`~/.codex`、通常workspace、他project state、他project handover、Podman socketをcontainerへ渡さない。
- Phase 2の外向き通信はdomain allowlistされていない。結果に「制限済み」と記録しない。
- 観測にはsecret-freeなcommand分類、exit status、認証方式、mode/owner、mount分類、PASS/FAILだけを記録する。未実行の観測は`not run`のまま残す。
- GitHub mutationは別途利用者承認後だけ、非mainの専用test branchで実行する。commitは許可されてもpush/PRは別の承認対象であり、mergeしない。

## 承認付きチェックリスト

1. `podman info`でrootlessがtrueであることを確認する。
2. `bin/agentctl build`を実行し、exit 0、`localhost/agent-container:dev`、`codex --version`と`claude --version`の両方が成功すること、build出力にhost credential path/valueがないことを確認する。
3. Claude認証前に`bin/agentctl doctor PROJECT --agent claude`を実行する。missing stateは明示したFAILであり、tracebackやcredential本文を出さないことを確認する。
4. 利用者承認・同席で`bin/agentctl auth claude`を実行する。browser codeを記録せず、`.credentials.json`の通常file・owner・mode `0600`だけを確認する。`claude auth status`で認証状態だけを確認し、credential本文を表示しない。
5. disposableなClaude migration sourceを用意し、`bin/agentctl migrate claude PROJECT --from ABSOLUTE_PATH`のdry-runをreviewする。利用者承認後に`--apply`を一回だけ実行し、allowlistだけがproject別`claude-config`へ入ること、sourceが変わらないことを確認する。既存destinationへは適用しない。
6. `bin/agentctl doctor PROJECT --agent claude`を再実行する。必須checkがPASSで、network policyのWARNだけが残ることを確認する。必要なら`bin/agentctl doctor PROJECT --agent all`でCodexとClaudeの回帰も確認する。
7. Claude runtimeのmountをcredential本文を読まずに検査する。選択projectのworkspace、`claude-config`、単独`.credentials.json`、cache、read-only `gh`、対象handoverだけを確認し、host `~/.claude`、`~/.codex`、旧claude-container、Podman socket、他project stateがないことを確認する。
8. `bin/agentctl run PROJECT --agent claude`でClaudeを起動する。非mainの専用test branchで、利用者承認済みの小さな変更、test、local commitを行う。push、PR作成、merge、force-pushはこのcheckに含めない。
9. Claudeを通常終了して同じprojectを再起動し、Claudeの対話session resumeを確認する。他project stateやhost `~/.claude`を使ってresumeしない。
10. 認証更新を利用者承認付きで行う。前後で`.credentials.json`のowner/mode、mtime/inodeが変化したか、exit status、write/rename errorの有無だけを記録する。credential本文、生のtimestamp、sizeは記録しない。nested file mountでrefreshが失敗したら停止し、credentialをprojectごとにcopyしない。
11. `bin/agentctl run PROJECT --agent codex`、Codexの認証状態、既存testを確認し、Codex regressionがないことを確認する。
12. 旧claude-containerのGit statusと対象stateをsecret-freeに確認し、Phase 2実行前後で旧claude-containerが変更されていないことを証明する。旧claude-containerを変更しない。

## 観測結果

unit suiteの結果を実host観測として扱いません。実行後は、実施した行の`not run`だけを日付、exit code、secret-freeな証拠へ置換します。skipped行は`not run`のまま残します。

| command/check | expected result | observed result | date |
| --- | --- | --- | --- |
| `podman info` | rootless is true | not run | not run |
| image build and both versions | exit 0; local image; Codex and Claude versions; no credential path/value | not run | not run |
| pre-auth Claude doctor | explicit missing-state FAIL; no traceback or secret | not run | not run |
| approved Claude login | credential file exists with owner and `0600`; `claude auth status` only | not run | not run |
| disposable migration dry-run and apply | reviewed allowlist only; source unchanged; atomic destination | not run | not run |
| Claude doctor | required checks PASS; documented network WARN | not run | not run |
| Claude mount inspection | permitted project mounts only; prohibited host paths absent | not run | not run |
| Claude edit, test, commit | approved non-main test branch; local commit only | not run | not run |
| Claude restart and resume | same-project session continuity | not run | not run |
| credential refresh metadata | auth remains valid; metadata only; no write/rename error | not run | not run |
| Codex regression | Codex run/auth/test remain usable | not run | not run |
| old container unchanged | old claude-container Git/state unchanged | not run | not run |

merge、force-push、release、deletion、mainへのpushはこのsmoke testから除外します。
