# Phase 2 Claude Code 実host smoke test

このchecklistは、networked build、Claude setup-token認証、Podman host、Git操作を伴う実host確認です。unit testの代替ではありません。既存の観測結果、とくに2026-08-23のnested-mount失敗はhistorical diagnosisとして残し、新方式の結果で上書きしません。各外部操作は実行直前に利用者承認を得ます。

## 共通の安全規則

- credential本文を表示しない。token、`.credentials.json`、`.claude.json`、backup、`oauth-token`の本文、認証設定本文、環境変数値を記録しない。JSON parser、checksum、prefix確認、長さ表示も禁止する。
- tokenをchat、この会話、handover、shell history、screenshot、screen recording、log、command line、host環境へ渡さない。`claude setup-token`の表示とhidden pasteは利用者本人だけがprivate terminalで行い、Codexやcaptured automationでは実行しない。
- `mainへ直接pushしない`。merge、force-push、release、repository削除は行わない。
- hostの`~/.claude`、`~/.codex`、通常workspace、他project state、他project handover、Podman socketをcontainerへ渡さない。
- Phase 2の外向き通信はdomain allowlistされていない。結果に「制限済み」と記録しない。
- 観測にはsecret-freeなcommand分類、exit status、公開CLI version、exact path、file type、mode/owner、container/image identifier、mount分類、PASS/FAILだけを記録する。生のmtime、size、credential値・prefix・長さ・process environmentは記録しない。未実行の観測は`not run`のまま残す。
- GitHub mutationは別途利用者承認後だけ、非mainの専用test branchで実行する。commitは許可されてもpush/PRは別の承認対象であり、mergeしない。

## 公開preflightとmetadata snapshot

Codexはここから通常build完了までを実行してよい。認証やlive shared/project stateの移動は行わない。

1. 次を実行し、全testのPASSとwhitespace errorなしを確認する。

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   git diff --check
   ```

2. state変更の前に、状態rootと次のexact pathだけを確定する。存在するentryは`lstat`相当でpath、type、mode、numeric ownerだけを記録し、本文やsymlink targetを読まない。

   ```text
   <state-root>/shared-auth/claude/oauth-token
   <state-root>/shared-auth/claude/.credentials.json
   <state-root>/shared-auth/claude/.claude.json
   <state-root>/shared-auth/claude/backups
   <state-root>/projects/agent-container/claude-config/.credentials.json
   <state-root>/quarantine/claude
   ```

   metadata確認の形式は次に限定する。credential pathへ`cat`、`jq`、`sed`、JSON parser、checksum、prefix/length出力を使わない。

   ```bash
   stat --format='%n type=%F mode=%a owner=%u:%g' -- EXACT_PATH
   ```

3. 旧claude-containerをread-onlyに列挙し、対象container ID、name、image ID、image name、stateだけをsnapshotする。対象を停止、rename、remove、start、rebuildしない。対象をexact IDで確定してから、build前後に同じformatで比較する。

   ```bash
   podman ps -a --no-trunc --format '{{.ID}} {{.Names}} {{.ImageID}} {{.Image}} {{.State}}'
   podman inspect --format '{{.Id}} {{.Name}} {{.Image}} {{.State.Status}}' OLD_CONTAINER_ID
   podman image inspect --format '{{.Id}} {{.RepoTags}}' OLD_IMAGE_ID
   ```

4. 固定version optionを付けずに通常buildを実行する。

   ```bash
   bin/agentctl build
   ```

   Codex/Claude両方の既定値が`latest`で、毎回のcachebusterがCLI install layerをinvalidateしたこと、出力に現在解決された公開CLI versionがあることを確認する。`--codex-version`と`--claude-version`はrollback専用で、このbuildには使わない。build後に手順3を再実行し、旧container/imageのidentifierとstateが一致することを確認する。

## Private setup-token ceremony — 利用者本人だけが実行

ここからは必ず停止し、利用者へ引き渡す。利用者は同じworktreeと意図した絶対`AGENT_CONTAINER_HOME`を設定したprivate terminalで、次だけを実行する。

```bash
bin/agentctl auth claude
```

`claude setup-token`はprivate terminalに1年間有効なinference専用tokenを一度表示する。このcredentialではRemote Controlを利用できない。利用者は表示tokenを後続の`Paste Claude setup token (input hidden):` promptへだけ貼り付ける。token値をこのchecklist、chat、handover、shell history、screenshot、logへ転記しない。commandのexit statusだけを記録する。

setup command、hidden prompt、token format、staged `claude auth status`、activationのどれかが失敗または取消なら即座に停止する。以前の`oauth-token`がactiveのままであり、shared legacy entryとproject legacy entryが移動していないことをmetadataだけで確認する。手作業で上書き、copy、quarantine、rollbackを行わない。

成功時だけ、active `oauth-token`が通常file・`0600`・実行user所有であること、shared `.credentials.json`、`.claude.json`、`backups`がshared authから消え、`<state-root>/quarantine/claude/<run-id>/`のprivate treeへ移ったことをmetadataだけで確認する。quarantineを削除しない。

## 認証成功後のrecovery smoke

1. まず`bin/agentctl doctor agent-container --agent claude`を実行する。failed-smoke project credentialが残っている間は`claude-project-credentials`だけがFAILになることを確認し、`agent-container` runtimeはまだ起動しない。

2. `.credentials.json`が存在しない登録済みのclean smoke projectで次を実行する。

   ```bash
   bin/agentctl doctor CLEAN_PROJECT --agent claude
   bin/agentctl run CLEAN_PROJECT --agent claude
   ```

   doctorの必須checkがPASSであることを確認する。local statusだけでは不十分なので、launcher経由のClaudeでcredential値を含まない最小promptを送り、実API responseが成功することを必須とする。HTTP 401を含むinference失敗は停止条件とする。clean projectを用意できない場合は回復を先行せず停止する。

3. 同じclean projectのClaude Bash subprocessから、値や環境一覧を表示せず次のbooleanだけを確認する。

   ```text
   CLAUDE_CODE_OAUTH_TOKEN present: false
   Anthropic/cloud credential variables present: false
   ```

   `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`の効果とLinux subprocess PID isolationを確認する。環境entry、値、prefix、長さ、process environment、`/run/secrets/claude-oauth-token`本文は出力せず、containerの`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、tmpfsを弱めない。

4. 新auth、clean-project status、最小inference、subprocess scrubがすべて成功した後だけ、failed smokeで生成されたexact artifact `<state-root>/projects/agent-container/claude-config/.credentials.json`を回復する。sourceと新しいtarget `<state-root>/quarantine/claude-project/<run-id>/.credentials.json`の全ancestorを`lstat`相当で確認し、sourceが通常fileでない場合、またはsource/target/ancestorがsymlinkなら停止する。新しい`<state-root>/quarantine/claude-project/<run-id>/`をmode `0700`で作り、この1 fileだけを本文を読まずに移し、mode `0600`にする。project `.claude.json`、project backups、project cache、sessions、plugins、memoryは移動しない。quarantineは削除しない。

5. recovery後に次を実行し、`claude-project-credentials`を含む必須checkがPASSで、既知の`WARN network-policy`だけが残ること、実inferenceが成功することを確認する。

   ```bash
   bin/agentctl doctor agent-container --agent claude
   bin/agentctl run agent-container --agent claude
   ```

6. 非mainの専用test branchで、Claudeに承認済みの小さな変更、focused test、local commitを行わせる。通常終了後に同じprojectを再起動し、同じprojectのsessionをresumeできることを確認する。push、PR、merge、force-push、releaseは行わない。

7. project configに`.credentials.json`がないこと、共有されるClaude credentialは`oauth-token`だけであること、別projectからこのprojectのconfig/sessionを観測できないことをmetadataとbooleanだけで確認する。

8. `bin/agentctl doctor agent-container --agent all`、Codex auth status、Codex runtime、全test suiteを実行してCodex regressionがないことを確認する。最後に旧container/image invariantを再確認する。

## 旧nested-mount checklist（historical diagnosis）

1. `podman info`でrootlessがtrueであることを確認する。
2. `bin/agentctl build`を実行し、exit 0、`localhost/agent-container:dev`、`codex --version`と`claude --version`の両方が成功すること、build出力にhost credential path/valueがないことを確認する。
3. Claude認証前に`bin/agentctl doctor PROJECT --agent claude`を実行する。missing stateは明示したFAILであり、tracebackやcredential本文を出さないことを確認する。
4. 旧方式では利用者承認・同席でClaude loginを実行し、`.credentials.json`のmetadataと`claude auth status`だけを確認した。この方式は廃止済みであり、再実行しない。
5. disposableなClaude migration sourceを用意し、`bin/agentctl migrate claude PROJECT --from ABSOLUTE_PATH`のdry-runをreviewする。利用者承認後に`--apply`を一回だけ実行し、allowlistだけがproject別`claude-config`へ入ること、sourceが変わらないことを確認する。既存destinationへは適用しない。
6. `bin/agentctl doctor PROJECT --agent claude`を再実行する。必須checkがPASSで、network policyのWARNだけが残ることを確認する。必要なら`bin/agentctl doctor PROJECT --agent all`でCodexとClaudeの回帰も確認する。
7. 旧nested-mount方式のClaude runtime mountをcredential本文を読まずに検査した。単独`.credentials.json` mountは失敗原因であり、再採用しない。
8. `bin/agentctl run PROJECT --agent claude`でClaudeを起動する。非mainの専用test branchで、利用者承認済みの小さな変更、test、local commitを行う。push、PR作成、merge、force-pushはこのcheckに含めない。
9. Claudeを通常終了して同じprojectを再起動し、Claudeの対話session resumeを確認する。他project stateやhost `~/.claude`を使ってresumeしない。
10. 旧nested credential file mountの認証更新は実行しない。refresh失敗時も共有config全体のmountやprojectごとのcredential copyへ緩和しない。
11. `bin/agentctl run PROJECT --agent codex`、Codexの認証状態、既存testを確認し、Codex regressionがないことを確認する。
12. 旧claude-containerのGit statusと対象stateをsecret-freeに確認し、Phase 2実行前後で旧claude-containerが変更されていないことを証明する。旧claude-containerを変更しない。

## 観測結果

unit suiteの結果を実host観測として扱いません。実行後は、実施した行の`not run`だけを日付、exit code、secret-freeな証拠へ置換します。skipped行は`not run`のまま残します。

| command/check | expected result | observed result | date |
| --- | --- | --- | --- |
| setup-token automated preflight | complete suite PASS; no whitespace errors | unittest exit 0; `Ran 194 tests`; `OK`; `git diff --check` exit 0 | 2026-08-24 |
| normal latest rebuild | no fixed version flags; cachebuster invalidates CLI install; both public versions resolve | `bin/agentctl build` exit 0; CLI install step executed; `localhost/agent-container:dev` image `f9741ac54634779a6ec86b78c458c05c35143691b0a74d29f4d1c1321602d75c`; Codex `0.149.1`; Claude `2.1.241` | 2026-08-24 |
| private setup-token ceremony | user-only private terminal; exit status and sanitized result only | user reported exit 0; controller received no token value; active `oauth-token` metadata is regular file, mode `0600`, owner `1000:1000`; shared legacy names absent; new private quarantine tree has `0700` directories and `0600` files | 2026-08-24 |
| post-auth Claude doctor | authenticated status; expected failed-project credential identified separately | exit 1; every required check PASS except `claude-project-credentials`; expected network-policy WARN only | 2026-08-24 |
| real inference and subprocess scrub | successful API response; both credential-presence booleans false | not run; blocked because `agent-container` is the only registered project and its exact legacy credential entry is present; no clean-project candidate exists | 2026-08-24 |
| failed-project credential quarantine | exact artifact only; private metadata; no deletion | not run | not run |
| `podman info` | rootless is true | PASS via doctor; exit 0 | 2026-08-23 |
| image build and both versions | exit 0; local image; Codex and Claude versions; no credential path/value | exit 0; `localhost/agent-container:dev`; Codex `0.149.0`; Claude `2.1.241` | 2026-08-23 |
| pre-auth Claude doctor | explicit missing-state FAIL; no traceback or secret | exit 1; Claude auth/config missing only, no traceback/secret | 2026-08-23 |
| approved Claude login | credential file exists with owner and `0600`; `claude auth status` only | user completed; exit 0; metadata only (`0600`), controller did not capture browser code or credential body | 2026-08-23 |
| disposable migration dry-run and apply | reviewed allowlist only; source unchanged; atomic destination | dry-run/apply exit 0; four allowlist files only; destination created atomically; source unchanged | 2026-08-23 |
| Claude doctor | required checks PASS; documented network WARN | exit 0; required checks PASS; network policy WARN only | 2026-08-23 |
| Claude mount inspection | permitted project mounts only; prohibited host paths absent | runtime target file check exit 0, but nested-mount `claude auth status` exit 1; root cause evidence: whole shared auth config status passes while project config lacks shared `oauthAccount` metadata; runtime created project `.credentials.json`/`.claude.json`/`backups`/`cache`, so isolation check FAIL and Phase 2 stopped | 2026-08-23 |
| Claude edit, test, commit | approved non-main test branch; local commit only | not run; stopped before edit because runtime auth/isolation check failed | 2026-08-23 |
| Claude restart and resume | same-project session continuity | not run | not run |
| credential refresh metadata | auth remains valid; metadata only; no write/rename error | not run | not run |
| Codex regression | Codex run/auth/test remain usable | not run | not run |
| old container/image unchanged | no old container stop/rename/remove/rebuild; legacy image IDs/tags unchanged | read-only inventory exit 0 before/after; no container records existed in either snapshot; image `771f5f00d0c0d375e9999d7a04b02c3b5784d22d646e3c7a60647db8c6afd1a4` retained tag `localhost/dotclaude-ops-4ee81d2d_claude-auth-workspace:latest`; image `8e1b09df94dad80925d9b820289d232a69a617592a18445548c1049f673cf2e8` retained tag `localhost/claude-container-3f484613_claude-auth-workspace:latest` | 2026-08-24 |

merge、force-push、release、deletion、mainへのpushはこのsmoke testから除外します。
