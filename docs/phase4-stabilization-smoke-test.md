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

repository bindingはproject-scopedで、新規登録には`--github-repository-id`を必須とする。旧schema policyのlegacy global fallbackは既存project互換性だけのために残る。shared installationはproduction and smoke selected repositoriesの両方を維持する。Do not deselect the production repository。each installation token narrows to exactly one project repository IDであり、local doctorはremote App selectionを証明しない。

## 1. Scope reconciliation

- 初期設計、README、operator guideが、shipped interfaceを選択中repositoryのIssue list/view read-onlyと一致させていることを確認する。
- family Issue create/commentは開発repository brokerと権限を共有しない将来Phaseのfamily専用設計へ延期されていることを確認する。
- domain allowlist／egress controlはPhase 4に含めず、既知WARNとして独立した将来設計へ延期されていることを確認する。

## 2. Fixture repository inventory

### Reviewed candidateの再buildと確認

partial stateを読む前に、review済みcommitを含むhost checkoutでcandidateを再buildし、image内のversionと固定CLI entrypointを確認する。どれか一つでも失敗したら自動retryしない。

```bash
set -eu
bin/agentctl build >/dev/null
podman run --rm localhost/agent-container:dev python3 -m agent_container.agentctl --version >/dev/null
podman run --rm localhost/agent-container:dev agent-github --help >/dev/null
printf '%s\n' 'reviewed_candidate_valid=true'
```

### 観測済みの初回失敗

2026-08-29の初回登録はtoken発行後の`upload-discovery`で失敗した。bounded host診断で、global App metadataのrepository IDがsmoke repositoryの`jj1xgo/agent-container-smoke`と一致せず、別のselected repositoryへ限定されたtokenがsmoke projectのreadに使われたことを原因として確認した。これはremote App selection、permission、ruleset、Git/PR gateのPASSを意味しないため、下の記録行は`not run`のまま維持する。

失敗後に観測したpartial stateは、mode `0700`のproject directory、mode `0600`の旧schema broker policy、既存のmode `0600` `smoke-fixtures.json`、mode `0700`のhandover directoryである。`project.json`とworkspaceは作成されていない。project-scoped fix、test、reviewが完了しても自動retryせず、直前のread-only inventoryでこのexact stateを再確認し、同じrepositoryとclone影響を示す新しいhost承認を得る。

既定のprivate repositoryは`jj1xgo/agent-container-smoke`とする。作成、GitHub App installationへの追加、fixture Issue／label／milestone／Pull Requestの準備はそれぞれ実行直前の利用者承認を必要とし、repositoryを自動削除しない。fixture projectのhandover directory作成とbroker project登録も、exact repository、project ID、cloneによる外部状態、既存stateを再利用しないことを示したhost承認を実行直前に得る。

- `main`は初期READMEだけを持つ。open Issueは固定title、body、label、milestoneを持ち、closed Issueはviewのstateとbody確認に使う。
- open Pull RequestはIssue listから除外される固定sentinelを持つ。Issue／PR番号と期待値はhost側の`$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`へ、ownerが実行userのmode `0600`通常fileとして記録し、containerへmountしない。
- GitHub Appは`Only select repositories`でproduction repositoryを選択したまま、このexact smoke repositoryも選択し、Metadata read、Contents write、Pull requests write、Checks read、Issues readだけを付与する。全branch rulesetはforce-pushを禁止しbypassを持たない。

通常の新規登録では、同じproject ID、workspace、handover directory、broker project recordが存在するcollisionをread-onlyで確認する。いずれかが存在する場合は停止し、既存stateを削除、上書き、再利用、別repositoryへ再割当てしない。

今回のretryだけは、上記の観測済みpartial stateからの限定upgrade gateを使う。次のcheckがすべて成功し、policy内のrepository、default branch、protected branches、ruleset確認が要求値とexact matchすることをsecret値を出力せず検査できた場合だけ先へ進む。余分・不足・symlink・owner/mode mismatch・malformed stateがあれば変更せず停止する。

```bash
export AGENT_HANDOVER_ROOT="$HOME/handovers"
test -d "$AGENT_CONTAINER_HOME/projects/agent-container-smoke" || exit 1
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke" || exit 1
test "$(stat -c '%a' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke")" = 700 || exit 1
test "$(stat -c '%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke")" = "$(id -u)" || exit 1
test -f "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json" || exit 1
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json" || exit 1
test "$(stat -c '%a' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json")" = 600 || exit 1
test "$(stat -c '%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json")" = "$(id -u)" || exit 1
test -f "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json" || exit 1
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json" || exit 1
test "$(stat -c '%a' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json")" = 600 || exit 1
test "$(stat -c '%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json")" = "$(id -u)" || exit 1
test -d "$AGENT_HANDOVER_ROOT/agent-container-smoke" || exit 1
test ! -L "$AGENT_HANDOVER_ROOT/agent-container-smoke" || exit 1
test "$(stat -c '%a' "$AGENT_HANDOVER_ROOT/agent-container-smoke")" = 700 || exit 1
test "$(stat -c '%u' "$AGENT_HANDOVER_ROOT/agent-container-smoke")" = "$(id -u)" || exit 1
test ! -e "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json" || exit 1
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json" || exit 1
test ! -e "$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke" || exit 1
test ! -L "$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke" || exit 1
printf '%s\n' 'partial_state_filesystem_valid=true'
```

次にreview済みcodeのstrict loaderでlegacy policyを検証し、fixture manifestをexact schema、repository、default branch、positive Issue／PR integers、expected sentinelsまで検証する。bodyやIDは表示せずboolean markerだけを出す。

```bash
AGENT_CONTAINER_HOME="$AGENT_CONTAINER_HOME" PYTHONPATH=src python3 - <<'PY'
import json
import os
from pathlib import Path

from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_runtime import load_broker_policy
from agent_container.state import ProjectRecord, Repository


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate fixture key")
        value[key] = item
    return value


root = Path(os.environ["AGENT_CONTAINER_HOME"])
project_dir = root / "projects/agent-container-smoke"
repository = Repository.parse("jj1xgo/agent-container-smoke")
record = ProjectRecord(repository, Path("/unused"))
policy = load_broker_policy(
    project_dir / "github-broker.json", record, "agent-container-smoke"
)
expected_policy = BrokerPolicy.create(
    project_id="agent-container-smoke",
    repository="jj1xgo/agent-container-smoke",
    default_branch="main",
    protected_branches=("main",),
)
if policy.repository_id is not None or policy != expected_policy:
    raise ValueError("legacy policy mismatch")
print("legacy_policy_valid=true")

payload = json.loads(
    (project_dir / "smoke-fixtures.json").read_bytes(),
    object_pairs_hook=unique_object,
)
expected_static = {
    "repository": "jj1xgo/agent-container-smoke",
    "default_branch": "main",
    "open_body_sentinel": "phase4-open-body-sentinel",
    "closed_body_sentinel": "phase4-closed-body-sentinel",
    "excluded_field_sentinel": "phase4-excluded-field-sentinel",
    "pull_request_sentinel": "phase4-pr-exclusion-sentinel",
}
expected_keys = set(expected_static) | {
    "open_issue",
    "closed_issue",
    "pull_request",
}
if not isinstance(payload, dict) or set(payload) != expected_keys:
    raise ValueError("fixture schema mismatch")
if any(payload[key] != value for key, value in expected_static.items()):
    raise ValueError("fixture identity mismatch")
expected_numbers = {
    "open_issue": 1,
    "closed_issue": 2,
    "pull_request": 3,
}
if any(payload[key] != value for key, value in expected_numbers.items()):
    raise ValueError("fixture number mismatch")
print("fixture_manifest_valid=true")
PY
```

repository ID lookupもread-onlyだが、値がtranscriptへ出ないようtracingを先に無効化し、専用`GH_CONFIG_DIR`から変数へ直接取り込む。

```bash
set +x
set -eu
smoke_repository_id=$(
  GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh" \
    gh repo view jj1xgo/agent-container-smoke \
      --json databaseId --jq .databaseId
)
case "$smoke_repository_id" in
  ''|*[!0-9]*) exit 1 ;;
esac
test "$smoke_repository_id" -gt 0 || exit 1
printf '%s\n' 'smoke_repository_id_valid=true'
# STOP: fresh approval required before registration.
```

ここで必ず停止する。filesystem／policy／manifest／ID validityのboolean evidenceを示し、exact repository、project ID、legacy policy atomic upgrade、broker clone、project metadata作成、sibling manifest保持だけを対象とするfresh approvalを得る。fixture、production repository、App selection、ruleset、releaseのmutationは含めない。

### Fresh approval後の一度だけの登録

承認後だけ次の別blockを一度実行する。失敗時も先にIDをunsetしてから停止し、自動retryしない。shell tracingはIDのunset後まで再開しない。

```bash
if ! bin/agentctl project add jj1xgo/agent-container-smoke \
  --project agent-container-smoke \
  --handover-root "$AGENT_HANDOVER_ROOT" \
  --github-broker \
  --github-repository-id "$smoke_repository_id" \
  --default-branch main \
  --protected-branch main \
  --confirm-force-push-ruleset; then
  unset smoke_repository_id
  exit 1
fi
unset smoke_repository_id
# shell tracing may resume only after the ID is unset
```

`gh repo view ... --json databaseId --jq .databaseId`はexact repositoryだけのbounded host inventoryである。取得IDはCLIへ明示的に渡すが、broker audit、container output、container mountへ書かない。このcommandと登録retryは同じ承認ではなく、登録retryには実行直前のfresh approvalが必要である。

## 3. Local doctor and credential non-exposure

rootless Podmanとrebuild済みimageを確認し、project metadata、workspace origin、broker policyがexact repositoryと一致することを確認する。

```bash
bin/agentctl doctor agent-container-smoke --github-broker
bin/agentctl doctor agent-container-smoke --agent claude --github-broker
bin/agentctl doctor agent-container --github-broker
```

doctorのPASSはlocal App metadata、private key boundary、project policyだけを意味し、remote App selection、GitHub installation、permission、repository identity、ruleset、network到達性の確認ではない。explicit policyでは`project repository binding valid`、旧schemaでは`legacy global repository binding valid`と区別するがnumeric IDは表示しない。runtime内で`GH_CONFIG_DIR`、`GITHUB_TOKEN`、`GH_TOKEN`、host Git credential store、SSH agent、hostの`.config/gh`、App private key、`app.json`が利用不能であり、project別socketとephemeral capabilityだけがread-only mountされることを値を表示せず確認する。

## 4. Git/PR gate

mainへ直接pushしない。正の操作は一意なwork branchだけで行う。`git push`の直前に、exact repositoryと`test/github-broker-smoke-UNIQUE`だけを対象とするhost承認を得る。`agent-github pr create`の直前にも、同じrepository・branch・smoke PRだけを対象とする別の外部状態承認を得る。

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

gate完了後、smoke PRをcloseし、対応する一意な`test/github-broker-smoke-UNIQUE` branchだけを削除する場合は、broker操作と混同しないhost `gh` administrationとして、PR番号、repository、branchを示した別の実行直前host承認を得る。固定fixture Issue、Pull Request、`main`、他branchは変更・削除しない。

```bash
gh pr close PR_NUMBER \
  --repo jj1xgo/agent-container-smoke \
  --delete-branch
```

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
