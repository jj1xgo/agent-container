# Phase 4 scope整合・残存gate・v0.4.0 smoke test

承認済みの[Phase 4安定化設計](superpowers/specs/2026-08-29-phase4-stabilization-release-design.md)に従い、scope整合と専用fixture repositoryでのみ実施する残存実host gateを記録する。実行前に、対象、目的、影響を示して操作ごとの利用者承認を得る。未観測の行をPASSへ変更しない。

## 停止条件

次のいずれかで直ちに停止し、sandbox、mount、permission、network、credential境界を弱めるfallbackや再試行をしない。

- private key、JWT、installation token、capability、Authorization headerの値・長さ・prefix・suffix・hashをstdout、stderr、log、handover、command lineへ出した。
- containerのenvironment、filesystem、process argv、`/proc/*/environ`からcredentialを読めた。
- exact repository以外へaccessできた、またはprotected branchへのpush、ref delete、generic API、merge、releaseがbroker経由で可能だった。
- broker failureがlegacy `gh` credential、environment token、SSH agent、host credential helperへのfallbackを起動した。
- GitHub側の全branch force-push禁止rulesetとbypassなしを確認できない。

credential本文を表示しない。記録するのは固定schemaの結果、boolean、exit status、operation名、repository slug、PR番号などsecret-free metadataだけとする。host `gh` administrationによるfixture準備はcontainer broker operationsと別の承認・記録対象であり、失敗した外部操作を自動再実行しない。

## 1. Scope reconciliation

- 初期設計、README、operator guideが、shipped interfaceを選択中repositoryのIssue list/view read-onlyと一致させていることを確認する。
- family Issue create/commentは開発repository brokerと権限を共有しない将来Phaseのfamily専用設計へ延期されていることを確認する。
- domain allowlist／egress controlはPhase 4に含めず、既知WARNとして独立した将来設計へ延期されていることを確認する。

## 2. Fixture repository inventory

既定のprivate repositoryは`jj1xgo/agent-container-smoke`とする。作成、GitHub App installationへの追加、fixture Issue／label／milestone／Pull Requestの準備はそれぞれ実行直前の利用者承認を必要とし、repositoryを自動削除しない。

- `main`は初期READMEだけを持つ。open Issueは固定title、body、label、milestoneを持ち、closed Issueはviewのstateとbody確認に使う。
- open Pull RequestはIssue listから除外される固定sentinelを持つ。Issue／PR番号と期待値はhost側の`$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`へ、ownerが実行userのmode `0600`通常fileとして記録し、containerへmountしない。
- GitHub Appは`Only select repositories`でこのexact repositoryだけを選択し、Metadata read、Contents write、Pull requests write、Checks read、Issues readだけを付与する。全branch rulesetはforce-pushを禁止しbypassを持たない。

## 3. Local doctor and credential non-exposure

rootless Podmanとrebuild済みimageを確認し、project metadata、workspace origin、broker policyがexact repositoryと一致することを確認する。

```bash
bin/agentctl doctor PROJECT --github-broker
bin/agentctl doctor PROJECT --agent claude --github-broker
bin/agentctl run PROJECT --github-broker
```

doctorのPASSはlocal App metadata、private key boundary、project policyだけを意味し、GitHub installation、permission、repository ID、ruleset、network到達性の確認ではない。runtime内で`GH_CONFIG_DIR`、`GITHUB_TOKEN`、`GH_TOKEN`、host Git credential store、SSH agent、hostの`.config/gh`、App private key、`app.json`が利用不能であり、project別socketとephemeral capabilityだけがread-only mountされることを値を表示せず確認する。

## 4. Git/PR gate

mainへ直接pushしない。正の操作は一意なwork branchだけで行い、通常pushとPR createはそれぞれ直前の利用者承認を得る。

```bash
git fetch origin
git switch -c test/github-broker-smoke-UNIQUE
git commit --allow-empty -m "test: GitHub broker smoke"
git push -u origin test/github-broker-smoke-UNIQUE

agent-github pr create \
  --base main \
  --head test/github-broker-smoke-UNIQUE \
  --title "test: GitHub broker smoke" \
  --body "Phase 3 approved smoke test"

agent-github pr view PR_NUMBER
agent-github pr checks PR_NUMBER
```

clone/fetch、通常push、PR create/view/checksがbounded JSONで成功することを確認する。protected branch、delete、non-head ref、cross-repository、non-fast-forward操作はtest repository内で拒否されることを確認する。stale leaseは決定論的に同期できる方法が先に成立した場合だけ実hostで確認し、race依存の並行pushは使わない。merge、release、generic API interfaceは存在しない。smoke PRはmergeしない。

## 5. Issue data gate

```bash
agent-github issue list
agent-github issue view ISSUE_NUMBER
```

`issue list`はopen Issueだけを固定schemaで返し、Pull Request除外とそのsentinelの不在を確認する。`issue view`はopen／closed Issueの固定fieldとbodyを返す。milestoneなどの除外field sentinel、raw response、response header、credential由来情報をstdout、stderr、auditへ出さない。create、edit、comment、close、search、query、pagination、repository指定、generic APIはGitHub接続前に拒否し、別repository、別project、invalid number、oversize、malformed responseはfail closedとする。

## 6. Cleanup and stale client

runtime中にcapability本文や派生情報を表示しないstale clientを準備し、runtime終了後にsocketとcapability fileが残らず、そのclientのrequestが拒否されることを確認する。401時のtoken更新は自動testを必須証拠とし、実時間expiryを待つ長時間testはrelease必須gateにしない。

auditはraw行を無制限に転載せず、allowlist済みfieldだけを確認する。

```bash
jq -c '{timestamp,operation,status,stage}' \
  "$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl" | tail -n 5
```

## 7. Automated verification and review

release候補commitに対して全unit tests、GitHub／handover Unix socket integration、実Podman integration、documentation contract tests、`git diff --check`、image内version・実行file・Python source permission確認を実行する。credential、mount、network、filesystem、external-state、fallback、cleanup、rollbackを個別に評価する独立agent reviewでCritical／Important findingを残さない。

## 8. Release gate

scope整合の文書変更と必要なtest変更がmainへmerge済みであり、private repositoryの安全に実施可能な必須gate、全自動test、CI、独立reviewが成功していることを確認する。未実施項目には理由、影響、受容判断を記録し、`CHANGELOG.md`へ`v0.4.0`の変更と既知制約を記載する。release対象commitをcleanな`origin/main`で特定した後、利用者の最終承認を得て初めてannotated `v0.4.0` tagをpushし、同じtagからGitHub Releaseを作成する。公開済みtagは移動、上書き、再利用しない。

## 記録

実行後、該当する`not run`だけを日時、対象repository、期待結果、観測結果、`PASS`または`PARTIAL`へ証拠どおり置換する。途中の失敗を後続成功で隠さず、root causeと最終再実行を区別する。

| check | expected | observed | date |
| --- | --- | --- | --- |
| Scope reconciliation | initial design, README, and operator guide agree | not run | — |
| Fixture repository | private exact repository, fixtures, App selection, ruleset | not run | — |
| Git/PR gate | clone/fetch/push/PR succeed; negative operations denied | not run | — |
| Issue data gate | list/view/body fixed schema; Pull Request除外; excluded sentinel absent | not run | — |
| Cleanup/stale client | runtime artifacts removed and stale client denied | not run | — |
| Release gate | tests, review, CI, changelog, final approval, v0.4.0 | not run | — |
