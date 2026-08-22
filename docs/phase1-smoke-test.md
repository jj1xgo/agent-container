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

この表は実host smoke test専用です。2026-08-22に各項目のcontroller承認を得て実行しました。unit suiteの結果を実hostの観測結果として扱いません。

| command/check | expected result | observed result | date |
| --- | --- | --- | --- |
| `podman info` | rootless is true | exit 0; rootless `true` | 2026-08-22 |
| image build | exit 0; local image exists; no credential path/value | exit 0; `localhost/agent-container:dev` exists; credential path/valueなし; Debian bubblewrap 0.8.0を確認し、以後のTUI起動警告なし | 2026-08-22 |
| pre-auth doctor | explicit missing-state FAIL; no traceback or secret | exit 1; missing auth/project stateを明示; traceback・credential本文なし | 2026-08-22 |
| Codex device auth | auth file exists with `0600`; ChatGPT status only | exit 0; `auth.json`はmode `0600`、UID/GID 1000; login statusはChatGPT | 2026-08-22 |
| private clone and doctor | dedicated workspace; required checks PASS; documented network WARN | exit 0; 専用workspaceのoriginは正確なHTTPS URL; 必須項目は全PASS; network-policy WARNのみ | 2026-08-22 |
| container `gh auth status` | authenticated with masked token output only | exit 0; account認証済み・HTTPS; token表示はmasked | 2026-08-22 |
| container `codex login status` | authenticated without `auth.json` content | exit 0; ChatGPT認証済み; `auth.json`本文の表示なし | 2026-08-22 |
| TUI `/hooks` | SessionStart hook trusted | `agentctl run` exit 0; SessionStart hookを個別reviewし、trustedかつactiveを確認 | 2026-08-22 |
| statusline and `/status` | documented available fields are present | `agentctl run` exit 0; model・context・weekly・git・projectを表示; model+reasoning、context remaining、five-hour、weekly、used tokens、git branch、projectを設定（利用不能・ゼロの項目は省略） | 2026-08-22 |
| handover notification | latest path only; no body | `agentctl run` exit 0; `/handovers/agent-container/2026-08-22_1340.md`のpathと読取指示だけを通知; 本文なし | 2026-08-22 |
| restart and `/resume` | same project session continuity | `agentctl run` exit 0; 再起動後、同じproject sessionのresume成功 | 2026-08-22 |
| shared auth update | authentication remains valid; no bind-mount write error | 通常のnested mountでdevice auth exit 0; 前後ともmode `0600`、UID/GID 1000、size 4088; mtime changed、inode unchanged; 同じnested mountの`codex login status` exit 0、ChatGPT login有効; doctor exit 0、必須項目は全PASS・network-policy WARNのみ; write/rename errorなし | 2026-08-22 |
| approved branch push and PR | named test branch and unmerged PR created | exit 0; `test/phase1-container-smoke`をpush; [PR #2](https://github.com/jj1xgo/agent-container/pull/2)はOPEN・未merge、指定title、変更は`docs/phase1-container-smoke.md`のみ; remote mainに`tests`がなくunittest discoveryは開始前にexit 1; `git diff --check`はPASS | 2026-08-22 |
| container mount inspection | prohibited host paths and Podman socket absent | inspect exit 0; PASS; workspace、project Codex home、shared `auth.json`、project cache、専用`gh` read-only、対象handoverだけをmount; host通常`~/.codex`、既存開発worktree、他handover、Podman socketなし; 検査専用停止containerを削除 | 2026-08-22 |

今後チェックを再実行する場合も、command分類、exit status、secret-freeな結果、実施日だけを更新します。credential本文、token値、device code、生のmtimeは記録しません。
