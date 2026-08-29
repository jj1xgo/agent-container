# Phase 3 GitHub broker実装計画

**Goal:** 開発用GitHub credentialをcontainerへ渡さず、project限定のGit read、新しい作業branchの作成push、PR create/view/checksをhost broker経由で提供する。

**Architecture:** runtimeごと・projectごとのUnix socket brokerをhostで起動する。containerのremote helperと固定schema clientだけがsocketを利用し、GitHub App private keyとinstallation tokenはbroker memoryに限定する。Git pushはreceive-pack commandをbrokerが検査し、advertisementに存在しないunprotected branchをold OID zeroで作る場合だけ許可する。既存branchへのupdateを拒否し、fast-forwardもnon-fast-forwardもGitHubへ送信しない。追加作業は新しいbranchと必要に応じた新しいPRを使う。

2026-08-29 security correction: create-only enforcementはbrokerの不変条件であり、GitHub rulesetや有料planのbranch protectionに依存しない。新しいpolicyにruleset markerはなく、旧exact true-marker schemaだけをcompatibility inputとして読む。doctorはpaid GitHub settingを確認済みとはclaimしない。

**Tech Stack:** Python 3.11+標準library、Unix domain socket、HTTP/TLS、Git remote-helper protocol、Git pkt-line、GitHub App REST API、`unittest`、rootless Podman。

## 1. 実装原則

- TDDでpolicyとprotocol parserから実装する。
- secretをfixture本文として必要以上に扱わず、markerがoutputへ出ないnegative testを置く。
- socket、state、capabilityのfilesystem validationは既存のprivate path規則を再利用する。
- broker failureからlegacy `gh` mountへfallbackしない。
- 各sliceはnetwork不要のunit testを先に完成させる。
- 実GitHub Appとcredentialを使うtestは明示gateされたhost smokeに分離する。

## 2. Slice A: policy modelとframing

### Files

- Create: `src/agent_container/github_broker_policy.py`
- Create: `tests/container/test_github_broker_policy.py`

### Steps

1. safe project ID、repository、default/protected branchからimmutable `BrokerPolicy`を構築するtestを書く。
2. 許可operationを`git-upload-pack`、`git-receive-pack`、`pr-create`、`pr-view`、`pr-checks`へ固定する。
3. repositoryをrequest入力から選べず、policyとのexact matchだけを許可するvalidatorを実装する。
4. branch/ref、PR番号、title/body size、request byte上限のtable-driven testを追加する。
5. exceptionとrendered errorにcapabilityやsecret markerが含まれないことを確認する。

## 3. Slice B: pkt-lineとreceive-pack command parser

### Files

- Create: `src/agent_container/git_protocol.py`
- Create: `tests/container/test_git_protocol.py`

### Steps

1. pkt-line length、flush packet、最大packet、truncation、control byteを検査するbounded decoderを実装する。
2. SHA-1とSHA-256 object formatをadvertisementから確定し、OID長を固定する。
3. receive-packの`old OID SP new OID SP ref`と最初のcapability列をparseする。
4. delete、protected ref、`refs/heads/`外、duplicate ref、ref数超過、未知capabilityを拒否する。
5. advertisementのexact old OIDとのlease一致を検査する。
6. packfile本文はparseせず、command検査成功後だけbounded streamへ移行するstate machineをtestする。

## 4. Slice C: broker sessionとUnix socket

### Files

- Create: `src/agent_container/github_broker.py`
- Create: `src/agent_container/github_broker_protocol.py`
- Create: `tests/container/test_github_broker.py`

### Steps

1. version付きlength-prefixed request/response schemaを定義する。
2. project別run directory、socket、ephemeral capabilityを安全に作成・破棄する。
3. capability、project ID、operation、request sizeをrequestごとに検証する。
4. runtimeごとに一つのproject policyだけをloadする。
5. timeout、client切断、oversized request、別project、replay、broker再起動をfail closedにする。
6. secret-free JSON Lines audit writerを実装する。

## 5. Slice D: GitHub App token provider

### Files

- Create: `src/agent_container/github_app.py`
- Create: `tests/container/test_github_app.py`

### Steps

1. App metadataとprivate key pathのtype、owner、mode、symlinkを検証する。
2. RS256 JWTを生成する。標準libraryだけで安全に署名できないため、ここで唯一の暗号backendを選定し、dependency pinと供給chain reviewを別commitにする。
3. exact repository IDと必要permissionを明示してinstallation tokenを要求する。
4. tokenをmemoryだけにcacheし、有効期限前の安全marginで更新する。
5. 401時の一回だけの更新、TLS failure、redirect、rate limit、invalid JSONをtestする。
6. token formatのprefixや固定長に依存しないことをtestする。

## 6. Slice E: Git transport

### Files

- Create: `container/bin/git-remote-agent-broker`
- Create: `src/agent_container/git_remote_helper.py`
- Create: `tests/container/test_git_remote_helper.py`
- Update: `Containerfile`

### Steps

1. remote helperの`capabilities`と`connect git-upload-pack`を実装する。
2. URLのowner/repositoryをpolicyと照合し、arbitrary URLを拒否する。
3. full-duplex Unix socket streamを実装し、credentialを応答しないことをtestする。
4. broker側でGitHub smart HTTPのservice discoveryとupload-packを固定endpointへ接続する。
5. `connect git-receive-pack`をSlice Bのcommand gateへ接続する。
6. redirect、HTTP header、content type、response size、timeoutを制限する。

## 7. Slice F: PR client

### Files

- Create: `container/bin/agent-github`
- Create: `src/agent_container/github_client.py`
- Create: `tests/container/test_github_client.py`

### Steps

1. `pr create`、`pr view`、`pr checks`だけをparseするCLIを書く。
2. owner/repositoryとarbitrary API pathを引数に持たせない。
3. brokerで固定REST endpointとrequest schemaへ変換する。
4. merge、close、release、workflow、generic APIが存在しないnegative testを置く。
5. GitHub errorをsecret-freeな分類へ変換する。

## 8. Slice G: agentctl統合

### Files

- Update: `src/agent_container/state.py`
- Update: `src/agent_container/podman.py`
- Update: `src/agent_container/agentctl.py`
- Update: `tests/container/test_state.py`
- Update: `tests/container/test_podman.py`
- Update: `tests/container/test_agentctl.py`

### Steps

1. broker metadata、project policy、runtime state pathを`StateLayout`へ追加する。
2. `doctor PROJECT --agent ...`へlocal broker設定checkを追加する。
3. broker起動後だけruntime stateを作り、project別socketとcapabilityをmountする。
4. CodexとClaude双方から`gh` directory mount、`GH_CONFIG_DIR`、`gh auth git-credential`を削除する。
5. `project add`のcloneをbroker transportへ切り替える。
6. broker failure時にruntime、workspace mutation、legacy fallbackが起きない順序をtestする。

## 9. Slice H: migration、docs、実host gate

### Files

- Create: `docs/phase3-github-broker.md`
- Create: `docs/phase3-github-broker-smoke-test.md`
- Update: `README.md`
- Update: `CHANGELOG.md`
- Update: `.github/workflows/ci.yml`

### Steps

1. GitHub App作成、selected repository installation、permissionとbrokerのcreate-only制約を記載する。
2. broker切替と明示rollbackを記載する。
3. unit suiteへbroker testを追加し、credential不要のlocal socket integration testをCIへ追加する。
4. gated実host testでclone、fetch、push、PR、negative operation、token非露出を確認する。
5. gate成功後だけlegacy `gh` runtime mount削除をrelease対象として確定する。

## 10. Commit sequence

1. `test: define GitHub broker policy boundaries`
2. `feat: validate GitHub broker policy`
3. `test: define receive-pack command gate`
4. `feat: parse bounded Git receive-pack commands`
5. `feat: add project-scoped broker sessions`
6. `feat: mint scoped GitHub App tokens`
7. `feat: route Git transport through broker`
8. `feat: add brokered pull request operations`
9. `feat: integrate GitHub broker runtime`
10. `docs: document GitHub broker operations`

各commitで対象unit testと`git diff --check`を実行する。runtime mountを切り替えるcommitでは通常suiteに加えてrootless Podman integrationを必須とする。
