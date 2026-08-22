# Phase 1 実host smoke test

この手順は、認証済みTUIと外部サービスを使う実host確認です。unit testの代替ではありません。各外部操作は、実行直前に必要な承認を得てから行います。

## 共通の安全規則

- credential本文を表示しない。token、device code、`auth.json`本文、認証設定本文、環境変数値を記録しない。
- `mainへ直接pushしない`。merge、force-push、release、repository削除も行わない。
- hostの`~/.codex`、既存workspace、他projectのhandover、Podman socketがcontainer mountに含まれないことを確認する。
- Phase 1の外向き通信はdomain allowlistされていない。実施結果に「制限済み」と記録しない。
- 観測結果には、secret-freeなcommand分類、exit status、認証方式、path、PASS/FAILだけを記録する。未実行なら`not run`のまま残す。

## 承認付きチェックリスト

1. `podman info`を実行し、rootlessがtrueであることを確認する。
2. `bin/agentctl build`を実行し、exit 0、`localhost/agent-container:dev`の存在、およびbuild出力にhost credential path・値がないことを確認する。
3. 認証前に`bin/agentctl doctor agent-container`を実行し、欠けているstateを明示したFAILとして報告し、tracebackやcredential本文がないことを確認する。
4. 利用者同席で`bin/agentctl auth codex`を実行し、device codeをブラウザで完了する。`shared-auth/codex/auth.json`の存在とmode `0600`だけを確認する。`codex login status`でChatGPT authenticationを確認し、`auth.json`本文を読まない。
5. 専用workspaceへprivate repositoryをcloneする。

   ```bash
   bin/agentctl project add jj1xgo/agent-container \
     --handover-root "$HOME/obsidian-vault/handovers"
   bin/agentctl doctor agent-container
   ```

   clone先が専用状態rootであること、必須checkがPASSであること、network policyのWARNだけが残ることを確認する。
6. `bin/agentctl run agent-container`でcontainerを起動する。container内で`gh auth status`を実行し、masked token outputだけで認証方式を確認する。token値を記録しない。
7. container内で`codex login status`を実行し、`auth.json`内容を表示せずChatGPT loginを確認する。
8. Codex TUIで`/hooks`を確認してSessionStart hookをtrustする。`/statusline`と`/status`でmodel、context、利用limit、token、branch、projectの利用可能な表示fieldを確認する。最新handoverは本文ではなくpath通知だけであることを確認する。
9. Codexを通常終了し、containerを再起動して`/resume`を実行する。同じprojectのsession continuityを確認する。
10. 通常の認証済みCodex requestの前後で、`shared-auth/codex/auth.json`のowner、mode、size、modification timeだけを`stat`で記録する。`codex login status`を再実行し、bind-mount write/rename errorがないことを確認する。errorがあれば停止し、credentialを各projectへ複製しない。
11. GitHub mutationは別途利用者承認後にだけ行う。既存の同名branch/PRがないことを確認してから、`test/phase1-container-smoke` branch、利用者合意済みの非code marker、test、push、PR title `Phase 1 container smoke test`を作る。PRはmergeしない。
12. container mountを検査し、host `~/.codex`、host workspace、other handovers、Podman socketが存在しないことを確認する。

## 観測結果

この表は実host smoke test専用です。以下の項目はcontroller承認が必要なため、初期状態ではすべて未実行です。unit suiteの結果でこの表を更新しません。

| command/check | expected result | observed result | date |
| --- | --- | --- | --- |
| `podman info` | rootless is true | not run | — |
| image build | exit 0; local image exists; no credential path/value | not run | — |
| pre-auth doctor | explicit missing-state FAIL; no traceback or secret | not run | — |
| Codex device auth | auth file exists with `0600`; ChatGPT status only | not run | — |
| private clone and doctor | dedicated workspace; required checks PASS; documented network WARN | not run | — |
| container `gh auth status` | authenticated with masked token output only | not run | — |
| container `codex login status` | authenticated without `auth.json` content | not run | — |
| TUI `/hooks` | SessionStart hook trusted | not run | — |
| statusline and `/status` | documented available fields are present | not run | — |
| handover notification | latest path only; no body | not run | — |
| restart and `/resume` | same project session continuity | not run | — |
| shared auth update | authentication remains valid; no bind-mount write error | not run | — |
| approved branch push and PR | named test branch and unmerged PR created | not run | — |
| container mount inspection | prohibited host paths and Podman socket absent | not run | — |

When a controller-approved check runs, replace only its `not run` cell with a date, command category, exit status, and secret-free result. Keep every unexecuted row exactly `not run`.
