# Phase 3 GitHub App broker 運用ガイド

Phase 3では、GitHub credentialをcontainerへ渡さず、project限定のclone/fetch、作業branch push、Pull Request作成・閲覧・checks確認をhost側broker経由で提供します。現在は明示的な`--github-broker` opt-inです。brokerに失敗してもlegacy `gh` credentialへfallbackしません。

設計上のsecurity boundaryは[Phase 3設計](superpowers/specs/2026-08-25-phase-3-github-broker-design.md)を参照してください。実環境で有効化する前に[Phase 3 smoke test](phase3-github-broker-smoke-test.md)を完了します。

## 提供する操作と提供しない操作

broker modeで許可する操作は次だけです。

- exact repositoryのclone/fetch
- protected branch以外の`refs/heads/*`へのpush
- `agent-github pr create`
- `agent-github pr view`
- `agent-github pr checks`

repositoryはproject登録時のmetadataからhostが決定します。containerからowner、repository、任意URL、HTTP header、API pathを指定できません。merge、close、release、workflow dispatch、secret、environment、repository administration、generic API proxyは提供しません。

## GitHub Appの準備

GitHub Appは`Only select repositories`で対象repositoryだけへinstallします。App permissionは次に限定します。

| Permission | Level |
| --- | --- |
| Metadata | read |
| Contents | write |
| Pull requests | write |
| Checks | read |

Actions、Administration、Members、Secrets、Environments、Deployments、Workflows permissionは付けません。

GitHub側で全`refs/heads/**`へのforce pushを禁止するrulesetを有効にします。Git wire protocolのupdate commandだけではnew commitがold commitの子孫かbrokerが判定できないため、このrulesetは省略できません。brokerは別途、protected ref、delete、non-head ref、stale lease、未知capabilityを拒否します。

Appのclient ID、installation ID、対象repository IDとprivate keyを取得します。値をchat、handover、commit、shell historyへ貼り付けません。

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

`app.json`は次の固定schemaです。placeholderをGitHubで確認した実値へ置き換えます。

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

private key、App JWT、installation tokenは表示しません。installation tokenはexact repository IDと上記permissionを指定してbrokerが発行し、host memoryだけにcacheします。

## Broker modeでprojectを登録する

handover directoryを先に作り、rulesetをGitHub上で確認した後に登録します。

```bash
export HANDOVER_ROOT="$HOME/handovers"
mkdir -p "$HANDOVER_ROOT/REPOSITORY"

bin/agentctl project add OWNER/REPOSITORY \
  --handover-root "$HANDOVER_ROOT" \
  --github-broker \
  --default-branch main \
  --protected-branch main \
  --confirm-force-push-ruleset
```

`--confirm-force-push-ruleset`は実際に全branch force-push禁止rulesetを確認した場合だけ指定します。default branchは必ずprotected branchになります。`master`など他の保護対象があれば`--protected-branch`を繰り返します。

登録時のcloneはbroker経由です。broker起動、App state、policy、token発行、GitHub transportのどれかが失敗した場合、cloneやproject record作成を行わず、legacy `gh`へfallbackしません。

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

`doctor --github-broker`はlocal App metadata、private keyのfile boundary、project policyをread-onlyで検査します。`PASS github-broker: local App and project policy valid`はlocal stateだけの判定で、GitHub installation、permission、repository ID、ruleset、network到達性の実確認を意味しません。それらはsmoke testで確認します。

runtimeごとにproject別Unix socketとephemeral capabilityを生成し、read-only mountします。runtime終了時に両方を破棄します。broker modeでは`GH_CONFIG_DIR`、hostの`gh` state、credential helper、token環境変数をcontainerへ渡しません。

## GitとPull Request操作

workspaceでは通常のGit commandを使います。HTTPS originはruntimeのGit configでproject固定の`agent-broker://OWNER/REPOSITORY`へ転送されます。

```bash
git fetch origin
git switch -c feat/example
git push -u origin feat/example
```

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

outputはbrokerが検証したbounded JSONです。PR body、credential、GitHub error bodyをauditへ記録しません。PR番号、操作、成功/拒否、転送byte数だけをsecret-free auditへ記録します。

## 移行とrollback

既存projectをbroker modeへ暗黙移行しません。初期版では、App stateとrulesetを準備した上で、broker modeを使うprojectとして登録・実host smokeを行います。一つのruntimeへbrokerとlegacy `gh` credentialを同時に渡しません。

rollbackはbroker障害からの自動fallbackではなく、利用者が明示的に通常modeを選ぶ運用変更です。legacy `gh` stateは自動削除しません。ただし、brokerで登録したworkspaceをそのままcredential modeへ切り替えて安全だと推測せず、project metadata、origin、credential scopeをreviewします。

## Failure対応

- `github-broker` doctorがFAIL: file path、owner、mode、symlink、metadata schema、project policyを修正します。private key本文は表示しません。
- clone/fetch/push/PRが失敗: App installation、exact repository ID、permission、ruleset、networkをhost側で確認します。tokenやGitHub error bodyを採取しません。
- 401: brokerはcacheを破棄して一度だけtokenを更新します。繰り返しretryやlegacy fallbackを行いません。
- protected branch、delete、stale leaseの拒否: policyどおりです。制約を迂回せず、作業branchとPRを使います。
- broker停止後: Git/PR操作がfail closedになることが期待動作です。

認可後の既知の外部・protocol failureは、auditへ`status=error`と次の固定`stage`だけを記録します。

- `token`
- `upload-discovery`
- `upload-rpc`
- `receive-discovery`
- `receive-rpc`
- `pr-request`
- `response-stream`

1 connectionの既知failureはそのconnectionだけをfail closedにし、brokerは次のconnectionを受け付けます。認可違反は従来どおり`denied`であり、予期しないprogramming／listener／thread failureはruntime全体のfailureです。

診断時はraw auditを表示せず、allowlist済みfieldだけを選択します。

```bash
jq -c '{timestamp,operation,status,stage}' \
  "$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl" | tail -n 5
```

exception本文、GitHub response body、token、JWT、private key、capability、Authorization header、PR body、Git advertisement、packfile、commit内容を採取しません。

## 既知の境界

- 外向きnetworkはdomain allowlistされていません。
- receive-pack requestは現在最大256 MiBをmemoryへ集めてからcommand gateとGitHub POSTを行います。大きいpush向けのbounded streaming/spoolingは未実装です。
- `doctor`はGitHub側のpermissionやrulesetをnetwork検証しません。
- 実GitHub Appを使うsmokeはcredentialと外部状態変更を伴うため、利用者承認後にだけ実行します。
