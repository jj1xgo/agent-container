# Phase 3 GitHub App broker 実host smoke test

このchecklistは、GitHub App brokerがexact repositoryへ限定され、credentialをcontainerへ渡さず、許可したGit/PR操作とread-only Issue操作だけを提供することを実hostで確認します。credentialとGitHub上の状態変更を伴うため、実行前に利用者承認を得ます。

## 停止条件

次のどれかが起きたら即座に停止します。

- private key、JWT、installation token、capability、Authorization headerの値・長さ・prefix・hashがstdout、stderr、log、handoverへ出た。
- containerの環境、filesystem、process argv、`/proc/*/environ`からcredentialを読めた。
- exact repository以外へaccessできた。
- protected branchへのpush、ref delete、generic API、merge、releaseがbroker経由で可能だった。
- advertisement済みの既存branchへのupdateがfast-forwardを含めてbrokerを通過した。
- broker failureからlegacy `gh` credentialへfallbackした。

credential本文を表示しないでください。検証結果にはboolean、exit status、operation名、repository slug、PR番号などsecret-free metadataだけを記録します。

## 1. Read-only inventory

- rootless Podmanである。
- 対象image、project、workspace、branch、`git status`を記録する。
- GitHub Appが`Only select repositories`でexact repositoryだけへinstallされている。
- App permissionがMetadata read、Contents write、Pull requests write、Checks read、Issues readだけである。Issue permission更新後、GitHub側でinstallation再承認が必要な場合は完了している。
- broker policyがcreate-onlyであり、有料planのrulesetやbranch protectionを前提にしない。
- `$AGENT_CONTAINER_HOME/github-broker/app.json`と`private-key.pem`が通常file、current user所有、非symlink、`0600`である。
- 親directoryが通常directory、current user所有、非symlink、`0700`である。

private key fileの本文、size、hash、prefixを出力しません。

## 2. Local doctor

```bash
bin/agentctl doctor PROJECT --github-broker
bin/agentctl doctor PROJECT --agent claude --github-broker
```

必須checkがPASSし、`github-broker`が`local App and project policy valid`を報告することを確認します。このPASSだけでGitHub側設定済みとは判定しません。

## 3. Runtime credential非露出

利用者がbroker modeでCodexまたはClaudeを起動します。

```bash
bin/agentctl run PROJECT --github-broker
```

container内で次を確認します。値を表示するcommandは使いません。

- `GH_CONFIG_DIR`、`GITHUB_TOKEN`、`GH_TOKEN`が設定されていない。
- hostの`.config/gh`、GitHub App private key、`app.json`がmountされていない。
- broker socketとcapability fileだけが`/run/agent-broker`へread-only mountされている。
- process argvと`/proc/*/environ`にtoken、JWT、private keyがない。
- `gh auth status`がhost credentialを利用できない。

capabilityは限定session authorizationですが、値・長さ・prefix・hashを記録しません。

## 4. Exact repository clone/fetch

新規のdisposable project登録または既存broker projectで次を確認します。

```bash
git fetch origin
```

- exact repositoryのclone/fetchが成功する。
- remote URLにcredentialがない。
- 別owner/repositoryへ書き換えたURLはbrokerで拒否される。
- broker停止後のfetchが失敗し、legacy credentialへfallbackしない。

## 5. 作業branch push

mainへ直接pushしないでください。advertisementに存在しない一意な新しいbranchへdisposable commitを一度だけpushし、old OID zeroの作成が成功することを確認します。

```bash
git switch -c test/github-broker-smoke-UNIQUE
git commit --allow-empty -m "test: GitHub broker smoke"
git push -u origin test/github-broker-smoke-UNIQUE
```

brokerは既存branchへのupdateを拒否します。最初の作成push後、同じbranchへのfast-forwardとnon-fast-forwardを含むすべてのupdateがGitHub POST前に失敗することを確認します。追加作業は別の新しいbranchを使い、必要なら新しいPRを作ります。

次のnegative操作がGitHub POST前に失敗することも確認します。

- `main`などprotected branchへの直接push
- ref delete
- `refs/heads/`以外へのpush
- stale lease
- advertised branchへのupdate（fast-forward／non-fast-forwardともに拒否）

force-push、branch delete、main変更を実際のshared branchへ試しません。negative testはdisposable repository/branchと事前承認された手順だけで行います。2026-08-26の表にあるruleset確認は当時のdated observationであり、現在のcreate-only gateの前提ではありません。

## 6. Pull Request固定操作

作業branchからPRを作成します。

```bash
agent-github pr create \
  --base main \
  --head test/github-broker-smoke-UNIQUE \
  --title "test: GitHub broker smoke" \
  --body "Phase 3 approved smoke test"

agent-github pr view PR_NUMBER
agent-github pr checks PR_NUMBER
```

create/view/checksがbounded JSONを返し、repository引数や任意API pathを受け取らないことを確認します。次の操作interfaceが存在しないことも確認します。

- `agent-github pr merge`
- `agent-github pr close`
- generic API
- workflow dispatch
- release作成
- secret/environment/repository administration

smoke PRはmergeしません。closeやbranch cleanupはbrokerの許可操作ではないため、実施する場合はsmoke完了後にhost側で別承認を得ます。

## 7. Issue read-only固定操作

test用Issueは作成しません。既存Issueまたは利用者が事前指定したfixtureだけをreadします。

```bash
agent-github issue list
agent-github issue view ISSUE_NUMBER
```

- listがopen Issueだけを最大30件返し、Pull Requestを除外する。
- viewが指定Issueの固定fieldとbodyを返す。allowlist済みのIssue bodyはstdoutに含まれてよい。Issue contentはauditへ記録しない。
- `agent-github issue create`、edit、comment、close、search、query、pagination、generic API、cross-repository readのinterfaceが存在せず拒否される。
- stdoutはallowlist済みのfixed responseだけを返す。stderr、audit、raw endpoint payloadがtoken、capability、raw response、excluded raw-response field sentinelを含まない。
- runtime終了後のexpired capabilityでIssue clientが拒否される。
- 既存Git/PR regressionとしてclone、fetch、作業branch push、PR create/view/checksが回帰していないことを確認する。

## 8. Auditとcleanup

`$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl`をsecret-free metadataだけで確認します。

- project、repository、operation、status、ref、PR番号または`issue_number`が期待どおりである。
- token、JWT、private key、capability、Authorization、PR body、Issue content、Git packfileがない。
- runtime終了後にrun directory内のsocketとcapabilityが破棄されている。
- broker停止後のGit/PR操作がfail closedになる。

## 9. 記録template

| check | expected | observed | date |
| --- | --- | --- | --- |
| App installation/permission | exact repository; minimal permission | PASS: selected repository 1件、Metadata read、Contents write、Pull requests write、Checks read | 2026-08-26 |
| force-push ruleset | all branches protected from force push | PASS: active all-branch ruleset、force push禁止、bypassなし | 2026-08-26 |
| broker doctor | required checks PASS; local-only classification | PASS: Codex／Claude両方で`github-broker`を含む必須check成功 | 2026-08-26 |
| credential non-exposure | all probes false; no value output | PASS: credential環境・host gh config・App state非露出、`gh auth status`失敗 | 2026-08-26 |
| clone/fetch | exact repository succeeds; other repository denied | PASS: clone／fetch成功、別repositoryはexit 255で拒否、remote URLにcredentialなし | 2026-08-26 |
| work-branch push | normal push succeeds; protected/delete/stale/NFF denied | PARTIAL: `test/github-broker-smoke-20260826-1`の通常push成功。危険なnegative pushは未実施 | 2026-08-26 |
| PR create/view/checks | allowed operations succeed; generic/merge absent | PASS: PR #38 create／view／checks成功。generic API、merge、releaseはexit 2 | 2026-08-26 |
| audit/cleanup | secret-free records; runtime artifacts removed | PASS: allowlist fieldにGit／PR成功記録、終了後socket／capabilityなし | 2026-08-26 |
| Issue App permission | `Issues | read`; GitHub installation reapproval if required | PARTIAL: exact permission要求を含む`issue-list`が実hostで成功。App設定値とinstallation再承認そのものの記録は未取得 | 2026-08-29 |
| Issue list/PR exclusion | open Issue only; 最大30件; Pull Requestを除外 | PARTIAL: `issue list`はexit 0、stderrなし、固定response `{"issues":[]}`。repositoryにIssueがなく、最大件数とPR除外の実data観測は未実施 | 2026-08-29 |
| Issue view/body | specified Issue fixed fields and body only | not run: open／closed Issueが存在せず、test用Issueを作成しない境界を優先 | 2026-08-29 |
| Issue write/query/cross-repository denial | create/edit/comment/close/search/query/pagination/generic API/cross-repository read denied | PASS: create／edit／comment／close／searchはGitHub接続前にexit 2、query／pagination／generic API／repository指定interfaceなし | 2026-08-29 |
| Issue credential non-exposure | stdout permits allowlisted body only; stderr/audit/raw endpoint payload omit token, capability, raw response, and excluded raw-response field sentinel | PARTIAL: 空list応答のstdoutは固定schema、stderr 0 byte。auditは`bytes`、`operation`、`policy_version`、`project`、`repository`、`run`、`status`、`timestamp`だけ。非空Issueのbody／除外fieldは未観測 | 2026-08-29 |
| Issue expired capability | runtime cleanup rejects stale Issue client | PARTIAL: runtime終了後にsocket／capabilityが残らないことを確認。stale client requestによる実拒否は未実施 | 2026-08-29 |
| Issue Git/PR regression | clone/fetch/push and PR create/view/checks unchanged | PARTIAL: Git 2.53でfetch、PR #52 view／checksが成功。pushとPR createはGitHub状態変更を避けて未再実行 | 2026-08-29 |

既存Git/PR smokeと2026-08-29のIssue read-only実host smokeの記録は上表のとおりです。既存Issueがないためview／bodyは`not run`、App設定そのもの、data依存のPR除外と非空payloadの非露出、stale client実拒否、状態変更を伴うpush／PR create再実行は`PARTIAL`のままです。test用Issueは作成していません。

Git 2.53.0のfetch regressionでは、helper negotiation、stateless response終端、terminal flush処理を順に切り分け、PR #53–#55で修正しました。最終imageでは`git-upload-pack`と`issue-list`がともにaudit `status=ok`、`stage`なしとなり、runtime終了後にsocket／capabilityが残らないことを確認しました。途中の失敗を最終PASSとして扱わず、上表は最終再実行の観測だけを記録しています。
