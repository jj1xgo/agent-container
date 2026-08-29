# Phase 3 GitHub App broker 運用ガイド

Phase 3では、GitHub credentialをcontainerへ渡さず、project限定のclone/fetch、新しい作業branchの作成push、Pull Request作成・閲覧・checks確認、Issue一覧・詳細のreadをhost側broker経由で提供します。現在は明示的な`--github-broker` opt-inです。brokerに失敗してもlegacy `gh` credentialへfallbackしません。

repository bindingはproject-scopedです。global App stateはclient ID、installation ID、private keyを共有し、新規policyはprojectごとのrepository IDを保持します。旧schema policyだけは既存動作を保つlegacy global fallbackを使います。local inventoryやdoctorはremote App selectionを証明しません。

2026-08-29のscope整合として、shipped interfaceは選択中repositoryのIssue list/viewだけである。family Issue create/commentは開発repository brokerと権限を共有せず、将来Phaseのfamily専用設計へ延期する。fixture準備などで行うhost `gh` administrationはcontainer broker operationsから分離し、broker failureがhost `gh`、legacy credential、environment token、SSH agent、host credential helperへのfallbackを起動することはない。外向きnetworkのdomain allowlistは既知WARNとして維持し、独立した将来Phaseで設計する。

設計上のsecurity boundaryは[Phase 3設計](superpowers/specs/2026-08-25-phase-3-github-broker-design.md)を参照してください。実環境で有効化する前に[Phase 3 smoke test](phase3-github-broker-smoke-test.md)を完了します。

## 提供する操作と提供しない操作

broker modeで許可する操作は次だけです。

- exact repositoryのclone/fetch
- advertisementに存在しないprotected branch以外の`refs/heads/*`を作るpush
- `agent-github pr create`
- `agent-github pr view`
- `agent-github pr checks`
- `agent-github issue list`
- `agent-github issue view NUMBER`

`issue list`はopen Issueを作成日時の新しい順に最大30件返します。filter、sort、direction、page、limitのoptionはありません。`issue view`は1以上2,147,483,647以下の正の整数だけを受け取ります。repositoryはproject登録時のmetadataからhostが決定します。containerからowner、repository、任意URL、HTTP header、API pathを指定できません。Issue create、edit、comment、close、reopen、lock、unlock、delete、label／assignee変更、search、query、pagination、cross-repository read、merge、release、workflow dispatch、secret、environment、repository administration、generic API proxyは提供しません。

## GitHub Appの準備

GitHub Appは`Only select repositories`でproductionとsmokeの対象repositoriesだけへinstallします。shared installationはproduction and smoke selected repositoriesの両方を保持します。Do not deselect the production repository。each installation token narrows to exactly one project repository IDであり、project policyがtokenごとのexact repositoryを決定します。App permissionは次に限定します。

| Permission | Level |
| --- | --- |
| Metadata | read |
| Contents | write |
| Pull requests | write |
| Checks | read |
| Issues | read |

Actions、Administration、Members、Secrets、Environments、Deployments、Workflows permissionは付けません。Issue readを使う前にGitHub App側で`Issues | read`へ更新し、GitHub側で必要なinstallationの再承認を完了します。不足するpermissionでは別credentialやlegacy `gh`へfallbackしません。

push policyはcreate-onlyです。GitHubのreceive-pack advertisementに存在しないunprotectedな`refs/heads/*`で、commandのold OIDがzeroのときだけ新しいbranchを作れます。brokerは既存branchへのupdateを拒否し、fast-forwardもnon-fast-forwardもGitHub POST前に同じlocal gateで拒否します。protected ref、delete、non-head ref、stale lease、未知capabilityも拒否します。この境界はGitHub rulesetや有料planのbranch protectionに依存しません。

Appのclient ID、installation IDとprivate keyを取得します。対象repository IDはproject登録の直前に、下記のbounded host inventoryで取得します。値をchat、handover、commit、shell historyへ貼り付けません。

## Host private state

状態rootを設定し、private directoryを作ります。

```bash
export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"
install -d -m 0700 "$AGENT_CONTAINER_HOME" \
  "$AGENT_CONTAINER_HOME/github-broker"
```

次のexact pathへ2 fileを配置します。

```text
$AGENT_CONTAINER_HOME/github-broker/app.json
$AGENT_CONTAINER_HOME/github-broker/private-key.pem
```

`app.json`は次の固定schemaです。placeholderをGitHubで確認した実値へ置き換えます。`repository_id`は旧schema projectのlegacy global fallbackだけに使う互換fieldで、新規projectのbinding sourceではありません。

```json
{
  "client_id": "GITHUB_APP_CLIENT_ID",
  "installation_id": 12345678,
  "repository_id": 123456789
}
```

`client_id`はGitHub Appのclient IDであり、client secretではありません。`private-key.pem`にはGitHub Appからdownloadしたprivate keyを置きます。両fileを通常file、current user所有、非symlink、mode `0600`にします。親directoryは同じuser所有、非symlink、mode `0700`にします。

```bash
chmod 600 \
  "$AGENT_CONTAINER_HOME/github-broker/app.json" \
  "$AGENT_CONTAINER_HOME/github-broker/private-key.pem"
```

private key、App JWT、installation tokenは表示しません。installation tokenはproject policyのexact repository ID（legacy policyではglobal fallback ID）と上記permissionを指定してbrokerが発行し、host memoryだけにcacheします。

## Broker modeでprojectを登録する

handover directoryを先に作ります。hostのagent-container専用`gh` stateでexact smoke repositoryだけをqueryします。これはhost-only bounded REST inventoryであり、container brokerへgeneric APIを追加しません。`gh repo view --json id`のGraphQL node IDではなくRESTのnumeric `.id`を使います。shell tracingを先に無効化し、返された正のdecimal IDは表示せず変数へ保持します。

```bash
export HANDOVER_ROOT="$HOME/handovers"
test -d "$HANDOVER_ROOT/agent-container-smoke"

set +x
set -eu
smoke_repository_id=$(
  GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh" \
    gh api repos/jj1xgo/agent-container-smoke --jq .id
)
case "$smoke_repository_id" in
  ''|*[!0-9]*) exit 1 ;;
esac
test "$smoke_repository_id" -gt 0 || exit 1
printf '%s\n' 'smoke_repository_id_valid=true'
# STOP: fresh approval required before registration.
```

ここで停止し、exact repository、project ID、legacy policy atomic upgrade、broker clone、project metadata作成、既存sibling manifest保持を示してfresh approvalを取得します。App selection、fixture、GitHub repository setting、production repository、releaseのmutationは含めません。承認後だけ次の別blockを一度実行します。

```bash
if ! bin/agentctl project add jj1xgo/agent-container-smoke \
  --project agent-container-smoke \
  --handover-root "$HANDOVER_ROOT" \
  --github-broker \
  --github-repository-id "$smoke_repository_id" \
  --default-branch main \
  --protected-branch main; then
  unset smoke_repository_id
  exit 1
fi
unset smoke_repository_id
# shell tracing may resume only after the ID is unset
```

`--github-repository-id`は`--github-broker`専用で、新規broker projectでは必須です。IDはprojectのmode `0600` policyへ保存しますが、broker audit、container output、container mountへ書きません。新しいpolicy fileにruleset markerはなく、旧exact schemaの`ruleset_confirmed: true`はcompatibility inputとしてだけ読み取ります。default branchは必ずprotected branchになります。`master`など他の保護対象があれば`--protected-branch`を繰り返します。

登録時のcloneはbroker経由です。broker起動、App state、policy、token発行、GitHub transportのどれかが失敗した場合、cloneやproject record作成を行わず、legacy `gh`へfallbackしません。

recovery成功後はsmoke projectのCodex／Claude Codeとproduction projectの互換性をすべて確認します。

```bash
bin/agentctl doctor agent-container-smoke --github-broker
bin/agentctl doctor agent-container-smoke --agent claude --github-broker
bin/agentctl doctor agent-container --github-broker
```

## Doctorとrun

broker modeは各commandでも明示します。

```bash
bin/agentctl doctor PROJECT --github-broker
bin/agentctl run PROJECT --github-broker
```

Claude Codeの場合も同じbroker boundaryを使用します。

```bash
bin/agentctl doctor PROJECT --agent claude --github-broker
bin/agentctl run PROJECT --agent claude --github-broker
```

`doctor --github-broker`はlocal App metadata、private keyのfile boundary、project policyをread-onlyで検査します。新schemaなら`PASS  github-broker: local App and project repository binding valid`、旧schemaなら`PASS  github-broker: local App and legacy global repository binding valid`と表示します。どちらもlocal stateだけの判定であり、remote App selection、GitHub installation、permission、repository identity、GitHub branch setting、network到達性の実確認を意味しません。doctorは有料のGitHub設定を確認済みとはclaimせず、numeric repository IDを表示せず、network probeも行いません。

runtimeごとにproject別Unix socketとephemeral capabilityを生成し、read-only mountします。runtime終了時に両方を破棄します。broker modeでは`GH_CONFIG_DIR`、hostの`gh` state、credential helper、token環境変数をcontainerへ渡しません。

## Git、Pull Request、Issueの固定操作

workspaceでは通常のGit commandを使います。HTTPS originはruntimeのGit configでproject固定の`agent-broker://OWNER/REPOSITORY`へ転送されます。

```bash
git fetch origin
git switch -c feat/example
git push -u origin feat/example
```

このpush後に同じ`feat/example`を更新するpushは、fast-forwardでも拒否されます。追加作業は別の新しいbranchへpushし、必要なら新しいPRを作成します。

PR操作はcontainer内の固定schema CLIを使います。

```bash
agent-github pr create \
  --base main \
  --head feat/example \
  --title "feat: example" \
  --body "Summary and verification"

agent-github pr view 123
agent-github pr checks 123
```

Issue readもcontainer内の固定schema CLIだけを使います。

```bash
agent-github issue list
agent-github issue view NUMBER
```

list outputは`issues`配列だけで、各itemは`number`、`title`、`state`、`author`、`labels`、`created_at`、`updated_at`、`url`だけを含みます。GitHub list endpointが返すPull Request itemは除外します。view outputは同じfieldに`body`を加えます。bodyのGitHub `null`は空文字列へ正規化されます。outputはbrokerとclientが検証したbounded JSONをstdoutへ1行で出力し、credential、token、capability、raw response、response header、GitHub error bodyを含みません。

PR body、Issue title、body、author、label、URL、credential、GitHub error bodyをauditへ記録しません。Issue auditはoperation（`issue-list`または`issue-view`）、status、転送byte数、error時には失敗箇所に応じて`token`、`issue-request`、`response-stream`のいずれかの固定stageだけを記録し、viewの番号はPR番号と混在させず`issue_number`として記録します。

## 移行とrollback

既存projectをbroker modeへ暗黙移行しません。旧schema broker policyはread可能で、runtimeはglobal `app.json`のrepository IDを使うlegacy global fallbackを維持します。一つのruntimeへbrokerとlegacy `gh` credentialを同時に渡しません。

中断した新規登録の旧schema policyをproject-scopedへupgradeできるのは、`--github-repository-id`を明示し、repository、default/protected branchesと旧exact true markerが要求値に一致し、`project.json`とworkspaceがともに存在せず非symlinkで、policyがcurrent user所有のmode `0600`通常非symlink fileである場合だけです。旧markerはcompatibility確認にだけ使い、create-only enforcementを設定しません。既存sibling fileは保持し、既存の別ID、完成済みproject、workspace、mismatch、安全でないmetadata、malformed schemaではpolicyを変更せず停止します。retry前にはbounded read-only inventoryと新しいhost承認が必要です。

rollbackはbroker障害からの自動fallbackではなく、利用者が明示的に通常modeを選ぶ運用変更です。legacy `gh` stateは自動削除しません。ただし、brokerで登録したworkspaceをそのままcredential modeへ切り替えて安全だと推測せず、project metadata、origin、credential scopeをreviewします。

## Failure対応

2026-08-29のPhase 4初回登録ではtoken発行後の`upload-discovery`が失敗しました。観測された原因はglobal App metadataのrepository IDがsmoke repositoryと異なり、別のselected repositoryだけに限定されたtokenをsmoke projectへ使ったことです。これはremote App selectionやpermission gateの成功を意味しません。project-scoped bindingの実装後も、partial stateを再inventoryし新しいhost承認を得るまで登録をretryしません。

- `github-broker` doctorがFAIL: file path、owner、mode、symlink、metadata schema、project policyを修正します。private key本文は表示しません。
- clone/fetch/push/PR/Issue readが失敗: App installation、exact repository ID、permission、networkをhost側で確認します。tokenやGitHub error bodyを採取しません。
- 401: brokerはcacheを破棄して一度だけtokenを更新します。繰り返しretryやlegacy fallbackを行いません。
- protected branch、既存branch update、delete、stale leaseの拒否: policyどおりです。制約を迂回せず、新しいbranchと必要に応じた新しいPRを使います。
- broker停止後: Git/PR操作がfail closedになることが期待動作です。

認可後の既知の外部・protocol failureは、auditへ`status=error`と次の固定`stage`だけを記録します。

- `token`
- `upload-discovery`
- `upload-rpc`
- `receive-discovery`
- `receive-rpc`
- `pr-request`
- `issue-request`
- `response-stream`

1 connectionの既知failureはそのconnectionだけをfail closedにし、brokerは次のconnectionを受け付けます。認可違反は従来どおり`denied`であり、予期しないprogramming／listener／thread failureはruntime全体のfailureです。

診断時はraw auditを表示せず、allowlist済みfieldだけを選択します。

```bash
jq -c '{timestamp,operation,status,stage}' \
  "$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl" | tail -n 5
```

exception本文、GitHub response body、token、JWT、private key、capability、Authorization header、PR body、Issue content、Git advertisement、packfile、commit内容を採取しません。

## 既知の境界

- 外向きnetworkはdomain allowlistされていません。
- receive-pack requestは現在最大256 MiBをmemoryへ集めてからcommand gateとGitHub POSTを行います。大きいpush向けのbounded streaming/spoolingは未実装です。
- `doctor`はGitHub側のpermissionやbranch settingをnetwork検証しません。
- 実GitHub Appを使うsmokeはcredentialと外部状態変更を伴うため、利用者承認後にだけ実行します。
