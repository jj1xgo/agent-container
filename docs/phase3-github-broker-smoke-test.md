# Phase 3 GitHub App broker 実host smoke test

このchecklistは、GitHub App brokerがexact repositoryへ限定され、credentialをcontainerへ渡さず、許可したGit/PR操作だけを提供することを実hostで確認します。credentialとGitHub上の状態変更を伴うため、実行前に利用者承認を得ます。

## 停止条件

次のどれかが起きたら即座に停止します。

- private key、JWT、installation token、capability、Authorization headerの値・長さ・prefix・hashがstdout、stderr、log、handoverへ出た。
- containerの環境、filesystem、process argv、`/proc/*/environ`からcredentialを読めた。
- exact repository以外へaccessできた。
- protected branchへのpush、ref delete、generic API、merge、releaseがbroker経由で可能だった。
- broker failureからlegacy `gh` credentialへfallbackした。
- GitHub側で全branch force-push禁止rulesetを確認できない。

credential本文を表示しないでください。検証結果にはboolean、exit status、operation名、repository slug、PR番号などsecret-free metadataだけを記録します。

## 1. Read-only inventory

- rootless Podmanである。
- 対象image、project、workspace、branch、`git status`を記録する。
- GitHub Appが`Only select repositories`でexact repositoryだけへinstallされている。
- App permissionがMetadata read、Contents write、Pull requests write、Checks readだけである。
- 全`refs/heads/**`へのforce push禁止rulesetがactiveである。
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

mainへ直接pushしないでください。disposable commitを専用branchへ作り、通常pushを確認します。

```bash
git switch -c test/github-broker-smoke-UNIQUE
git commit --allow-empty -m "test: GitHub broker smoke"
git push -u origin test/github-broker-smoke-UNIQUE
```

次のnegative操作がGitHub POST前またはGitHub rulesetで失敗することを確認します。

- `main`などprotected branchへの直接push
- ref delete
- `refs/heads/`以外へのpush
- stale lease
- non-fast-forward push（全branch rulesetで拒否）

force-push、branch delete、main変更を実際のshared branchへ試しません。negative testはdisposable repository/branchと事前承認された手順だけで行います。

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

## 7. Auditとcleanup

`$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl`をsecret-free metadataだけで確認します。

- project、repository、operation、status、refまたはPR番号が期待どおりである。
- token、JWT、private key、capability、Authorization、PR body、Git packfileがない。
- runtime終了後にrun directory内のsocketとcapabilityが破棄されている。
- broker停止後のGit/PR操作がfail closedになる。

## 8. 記録template

| check | expected | observed | date |
| --- | --- | --- | --- |
| App installation/permission | exact repository; minimal permission | not run | YYYY-MM-DD |
| force-push ruleset | all branches protected from force push | not run | YYYY-MM-DD |
| broker doctor | required checks PASS; local-only classification | not run | YYYY-MM-DD |
| credential non-exposure | all probes false; no value output | not run | YYYY-MM-DD |
| clone/fetch | exact repository succeeds; other repository denied | not run | YYYY-MM-DD |
| work-branch push | normal push succeeds; protected/delete/stale/NFF denied | not run | YYYY-MM-DD |
| PR create/view/checks | allowed operations succeed; generic/merge absent | not run | YYYY-MM-DD |
| audit/cleanup | secret-free records; runtime artifacts removed | not run | YYYY-MM-DD |

この文書作成時点では実GitHub App smokeを未実施です。`not run`を成功へ書き換えず、実行日時、exit status、secret-free observationを確認後に記録します。
