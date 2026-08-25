# Phase 3 開発GitHub認証broker設計

- 日付: 2026-08-25
- 状態: Draft
- 対象: 自分の開発repositoryに対するclone、fetch、作業branchへのpush、PR操作

## 1. 背景

Phase 1とPhase 2では、agent-container専用のGitHub CLI設定directoryをruntimeへread-only mountし、`gh auth git-credential`をGit HTTPS credential helperとして使用している。この方式はhostの通常設定と分離できる一方、container内のagentまたはそのsubprocessがcredential helperを直接呼び出せるため、開発用credentialをcontainerへ渡さない境界にはなっていない。

Phase 3ではGitHub認証をhost側brokerへ移し、GitHub Appのprivate key、JWT、installation access tokenをcontainerへmount、環境変数、argv、stdin、応答本文のいずれでも渡さない。containerは選択中projectに必要なGit transportとallowlist済みGitHub操作だけをbrokerへ要求する。

GitHub App installation access tokenはrepositoryとpermissionを発行時に狭められ、有効期間は1時間である。brokerはこの短期tokenを必要時に発行し、失効または期限切れ後に再発行する。tokenの文字数、固定prefix、固定formatには依存しない。

## 2. 目標

- GitHub App private keyとinstallation access tokenをcontainerへ渡さない。
- 起動中projectのexact `OWNER/REPOSITORY`以外へbroker経由でアクセスできない。
- clone、fetch、作業branchへのpush、PRの作成・閲覧を日常利用できる。
- `main`など保護branchへの直接push、force-push、merge、release、repository削除、設定変更をbrokerの初期interfaceに含めない。
- request、診断、audit log、handoverへcredential本文を記録しない。
- broker停止時やpolicy不一致時は、既存のmounted `gh` credentialへfallbackせずfail closedにする。

## 3. 非目標

- GitHub全APIを透過proxyすること。
- arbitrary URL、arbitrary REST path、GraphQL query、`gh api`を通すこと。
- family repository用brokerと開発repository用brokerを同じ権限またはsocketで兼用すること。
- branch protectionやGitHub上のreview ruleをbrokerだけで置き換えること。
- hostの通常`~/.config/gh`、SSH agent、Git credential storeをcontainerへ渡すこと。

## 4. 信頼境界

### Host側で信頼するもの

- rootless userとして動くbroker process
- GitHub App ID、installation ID、private keyを保存するprivate state
- project登録時に確定したexact repository metadata
- broker自身の固定policyとGitHub TLS検証

### 信頼しないもの

- agentのprompt、生成command、subprocess、plugin、hook、MCP
- project workspaceのfile、Git config、hook、remote、object、ref
- containerから届くrepository名、branch名、HTTP header、Git protocol payload
- GitHubやnetworkから返るcredential以外の本文

brokerはcontainerと同じhost userで動くため、Unix socketのfile permissionだけをproject authorizationとは扱わない。起動時にprojectごとのcapabilityを生成し、socket自体もproject別directoryへ置き、requestごとにcapability、project ID、repositoryの一致を検証する。capabilityはcredentialではなくbrokerへの限定されたsession authorizationだが、ログやhandoverへ本文を出さず、runtime終了時に破棄する。

## 5. 採用architecture

```text
GitHub
  ^  HTTPS + installation access token
  |
host broker
  |- GitHub App private key（host private stateのみ）
  |- project/repository/permission policy
  |- short-lived installation token cache
  |- Git smart transport endpoint
  `- allowlist済みPR endpoint
       ^
       | project別Unix socket + ephemeral capability
       v
container
  |- git-remote-agent-broker
  |- agent-github pr create/view
  `- 選択project workspace
```

### 5.1 Git transport

container imageへ`git-remote-agent-broker` remote helperを配置する。workspaceのoriginはcredentialを含まないbroker schemeを使用する。

```text
agent-broker://OWNER/REPOSITORY
```

remote helperはGitの要求する`git-upload-pack`または`git-receive-pack` streamをproject別Unix socketへ中継する。helperとbrokerのprotocolはlength-prefixed messageとbounded byte streamとし、shell command、host path、HTTP URL、headerをcontainer側から指定させない。

brokerは登録済みrepositoryからGitHubのexact HTTPS endpointを構築し、installation tokenをAuthorizationへ注入する。redirectは原則拒否し、許可する場合も同一originの既知pathだけに限定する。TLS検証を無効化しない。

brokerはreceive-packのserver advertisementをcontainerへ転送した後、containerから届く最初のcommand packet列をpackfileより前に検査する。各commandの`old OID`、`new OID`、ref名と、最初のcommandに付くcapabilityだけをbounded parserで受け付ける。未知のobject format、壊れたpkt-line、push certificate、push optionは初期実装で拒否する。

初期push policyは次のとおりとする。

- delete refを拒否する。
- `main`、`master`とproject policyで指定したprotected refへのupdateを拒否する。
- `refs/heads/`以外へのpushを拒否する。
- commandの`old OID`が直前にGitHubからadvertiseされた同じrefのOIDと一致しない場合は拒否する。
- 一度のrequestで更新できるref数と転送量に上限を設ける。

Git wire protocolは更新commandにforce flagを持たず、`old OID`と`new OID`だけではnew commitがold commitの子孫か判定できない。brokerがpackfileを展開してuntrusted Git object graphを処理する方式は初期実装で採用しない。そのためnon-fast-forward拒否は、対象repositoryのGitHub rulesetで全`refs/heads/**`に対してforce pushを禁止することを必須条件とする。brokerのlease一致検査とGitHub rulesetを重ね、どちらかを省略した状態を「force-push拒否済み」と扱わない。`doctor`はruleset確認に必要なpermissionをbrokerへ追加せず、初期版では利用者がGitHub設定を確認したことをproject policyへ明示登録する。

receive-packのref update parserとGitHub ruleset確認が完成するまではpush対応を有効にしない。単なる認証付きbyte proxyを「branch制限済み」として公開しない。

host brokerはuntrusted workspaceで`git`、hook、filter、credential helperを実行しない。Git object処理はcontainer側Gitに残し、brokerはprotocol framing、policy検査、GitHubとのnetwork transportだけを担当する。

### 5.2 PR操作

genericな`gh` credentialやAPI proxyはruntimeへ渡さない。containerには固定schemaの`agent-github` clientを置き、初期interfaceを次に限定する。

- `pr create`: base、head、title、body
- `pr view`: project内のPR番号
- `pr checks`: project内のPR番号

repository owner/nameはrequest引数にせず、起動sessionへ紐づくproject metadataからbrokerが決定する。`pr create`のheadは作業branch、baseは既定でproject policyのdefault branchとし、文字数とUTF-8を検証する。merge、close、review dismissal、workflow dispatch、release、secret、environment、repository administration endpointは実装しない。

## 6. GitHub App permission

開発用GitHub Appは対象repositoryだけへinstallし、初期permissionを次に限定する。

| Permission | Level | 用途 |
| --- | --- | --- |
| Metadata | read | GitHub Appの必須metadata参照 |
| Contents | write | clone、fetch、作業branchへのpush |
| Pull requests | write | PR作成と閲覧 |
| Checks | read | PR check結果の閲覧 |

Actions write、Administration、Members、Secrets、Environments、Deployments、Workflowsは要求しない。新しい操作を増やす場合はApp permission、broker schema、audit項目、negative testを同じ変更でreviewする。

installation token作成時は、App installation全体の権限を暗黙に継承せず、exact repository IDと上記の必要permissionを毎回明示する。Appは`Only select repositories`でinstallする。

## 7. Stateとlifecycle

host stateは既定で次の構造にする。

```text
${AGENT_CONTAINER_HOME}/github-broker/
├─ app.json                 # App ID等の非secret metadata、0600
├─ private-key.pem          # private key、0600、非symlink
├─ audit/                   # secret-free JSON Lines、0700
└─ run/PROJECT/RUN_ID/      # runtime directory、0700
   ├─ broker.sock           # Unix socket
   └─ capability            # ephemeral、0600
```

private stateと全親directoryは通常directory、current user所有、非symlink、directory `0700`、file `0600`を必須にする。runtime directoryは予測困難なrun IDで作り、container終了後にsocketとcapabilityを破棄する。installation tokenはmemoryだけに保持し、disk cacheしない。

brokerは一つのruntimeに一つのproject capabilityを発行する。別projectのsocket、repository、run IDを指定したrequestは拒否する。broker再起動時は既存capabilityを無効化する。

## 8. Auditと診断

audit logへ記録してよい項目は次だけとする。

- UTC timestamp
- run IDの非可逆なlocal識別子
- project IDとrepository slug
- operation種別
- ref名またはPR番号
- allow/deny、終了status、転送byte数
- policy version

token、JWT、private key、Authorization header、request body、PR本文、Git packfile、commit内容、環境変数値は記録しない。診断はprivate keyの存在、type、owner、mode、App metadata、socket到達性、GitHub App installationとpermissionの一致だけを表示する。

`doctor`には`github-broker` checkを追加し、legacy `gh` mountがruntime specに残る場合はFAILとする。networkを伴うinstallation確認は明示的に区別し、local check成功だけでGitHub操作可能と断定しない。

## 9. Failure policy

- broker、socket、capability、GitHub App設定のいずれかが欠ければclone、run、push、PR操作を開始しない。
- token発行、TLS、Git protocol、policy parse、audit writeに失敗した場合は対象操作を拒否する。
- token期限切れの401は一度だけ新規tokenで再試行し、同じrequestを無制限に再送しない。
- GitHub rate limit、5xx、timeoutを権限不足として扱わず、secret-freeな分類で利用者へ返す。
- broker障害時に`gh` directory mount、host credential helper、環境変数token、SSH agent forwardingへfallbackしない。

## 10. Migration

1. brokerをread-only Git操作だけで導入し、既存`gh`方式と明示optionで切り替える。
2. exact repository制限、secret非露出、別project拒否を実hostで検証する。
3. receive-pack policy parserを追加し、作業branchへのnon-force pushだけを有効にする。
4. allowlist済みPR create/view/checksを追加する。
5. `project add`、Codex runtime、Claude runtimeから`gh` directory mountと`gh auth git-credential`を削除する。
6. 全登録projectの移行後、専用`gh` credentialを自動削除せずquarantineまたは手動rollback用に残す。

切替完了前は一つのruntimeへbroker socketとlegacy `gh` credentialを同時に渡さない。rollbackはhost側の明示設定変更とし、broker failureから自動fallbackしない。

## 11. Testと受け入れ条件

### Unit test

- project、repository、operation、ref、message sizeのallow/deny table
- path traversal、symlink、special file、owner、modeの拒否
- capability不一致、別project、期限切れrunの拒否
- tokenやprivate key markerがstdout、stderr、exception、auditへ出ない
- redirect、arbitrary URL、header injection、oversized payloadの拒否
- push delete、force、protected ref、non-head ref、複数ref上限超過の拒否
- runtime specに`gh` mount、`GH_CONFIG_DIR`、credential helper、token環境変数がない

### 実host test

- exact repositoryのcloneとfetchが成功する。
- 作業branchへの通常pushが成功する。
- `main`直接push、ref delete、別repository accessがbrokerで失敗する。
- non-fast-forward pushが必須GitHub rulesetで失敗し、broker auditにはGitHub拒否として記録される。
- PR create/view/checksが成功し、mergeとgeneric APIが利用できない。
- container内の環境、mount、process argv、helper応答、filesystemにtokenが存在しない。
- broker停止後にGit操作とPR操作がfail closedになる。
- token更新をまたぐ長時間sessionで再認証できる。

採用条件は、上記negative testを含む実host gateがすべてPASSし、legacy `gh` credentialをmountせずCodexとClaudeの両方から同じ制約で利用できることである。

## 12. 実装順序

1. broker protocol、policy model、private state validationを標準ライブラリで実装する。
2. project別Unix socket lifecycleとsecret-free auditを実装する。
3. GitHub App JWTとrepository-scoped installation token発行を実装する。
4. read-only Git remote helperとupload-pack transportを実装する。
5. receive-pack command parser、lease/ref policy、GitHub ruleset前提の検証を実装する。
6. PR create/view/checks clientを実装する。
7. `agentctl doctor`とruntime orchestrationへ統合する。
8. 実host security gate後にlegacy `gh` runtime mountを削除する。

## 13. 未決事項

- broker processをruntimeごとに起動するか、user serviceとして常駐させるか。初期実装はruntimeごとのprocessを優先する。
- PR本文の最大長とaudit上の識別方法。
- GitHub App作成・installationを`agentctl auth github-broker`で補助する範囲。private key自動生成やGitHub設定変更は初期CLIの対象外とする。

## 参考

- [GitHub App installationとして認証する](https://docs.github.com/en/enterprise-cloud@latest/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
- [installation access tokenを生成する](https://docs.github.com/en/enterprise-cloud@latest/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitHub App REST API](https://docs.github.com/en/rest/apps/apps)
- [Git remote helper protocol](https://git-scm.com/docs/gitremote-helpers)
- [Git pack protocol](https://git-scm.com/docs/pack-protocol)
