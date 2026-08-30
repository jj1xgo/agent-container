# Family Issue create broker 設計

- 日付: 2026-08-30
- 状態: 承認済み
- 対象: projectごとに明示登録したfamily repositoryへの、host承認付きIssue新規作成

## 1. 背景と目的

現行の開発GitHub brokerは、選択中の開発repositoryに対するGit transport、Pull Request操作、Issue read-only操作だけを提供する。Issueの作成、編集、comment、closeは意図的に提供せず、family repositoryへのwriteは異なる権限、threat model、運用を持つ将来機能として分離している。

この機能は、CodexまたはClaude runtimeがfamily repository向けのIssue案を固定schemaで提出し、host operatorが内容を確認して明示承認した場合に限り、Issueを1件作成できるようにする。開発repository用brokerへIssue write権限を追加せず、GitHub credentialをcontainerへ渡さず、agent単独では外部状態を変更できないことを安全境界とする。

## 2. Scope

### 2.1 含める

- projectごとにexactly oneのfamily repositoryをhost側で明示登録する。
- agentから`title`、`summary`、`context`、`acceptance criteria`の固定schemaを受け取る。
- credentialを持たないintake brokerがprivateなpending requestを作成する。
- host operatorがpending内容をpreviewし、request単位でapproveまたはrejectする。
- approve時だけ、family専用GitHub Appを使って登録済みrepositoryへIssueを1件作成する。
- GitHub送信結果が不明な場合の明示reconciliationを提供する。
- content-free audit、doctor、unit／integration／実host smoke、rollback手順を提供する。

### 2.2 含めない

- Issue comment、edit、close、reopen、delete。
- labels、assignees、milestone、project、attachment、discussion、sub-issue。
- 複数family repositoryのallowlist、runtimeによるrepository選択。
- 自由形式のGitHub API、GraphQL、generic REST proxy。
- 開発repository用GitHub App、token provider、broker socket、policyの再利用。
- agentによるapprove、reject、reconciliation。
- 承認なしの自動Issue作成、batch承認、包括的な事前承認。

## 3. Trust boundary

機能を次の2つへ分離する。

1. **Family intake plane**: runtimeから固定schemaを受け、pending requestを保存する。GitHub App metadata、private key、installation token、GitHub API clientを持たず、外向きnetwork通信を行わない。
2. **Family approval plane**: host operatorが明示的に起動する。pending requestとproject bindingを再検証し、family専用GitHub App credentialを必要時だけ読み、exact repositoryへIssue createを1回だけ要求する。

開発repository用GitHub brokerとfamily機能は、App、private key、installation、metadata、state directory、socket、capability、audit、runtime lifecycleを共有しない。共通化できるのはcredentialやpolicyを保持しない低水準の安全なvalidation／atomic filesystem helperに限り、どちらかのpolicy objectやrequest dispatcherをもう一方へ渡さない。

containerにはfamily App private key、JWT、installation token、repository名、repository ID、GitHub raw responseをmount、environment、argv、stdin、socket responseのいずれでも渡さない。

## 4. Project bindingとstate

各projectはhost state内にexactly oneのfamily bindingを持てる。bindingには正規化済み`owner/name`と、family専用App installationが返すrepository IDを保存する。登録時とapprove時にnameとIDの両方を照合し、rename、transfer、App selection変更をfail-closedで検出する。

概念上のstateは次のように分離する。

```text
family/
  app.json                 0600
  private-key.pem          0600
  projects/<project>/
    binding.json           0600
    pending/               0700
      <request-id>.json    0600
    audit/events.jsonl     0600
```

全ancestor directoryは期待ownerのreal directoryかつmode `0700`とし、fileはregular file、期待owner、mode `0600`とする。symlink、hard-link異常、FIFO、device、不明entry、path traversal、owner／mode不正を拒否する。書き込みは同一directory内のexclusive create、fsync、atomic renameを用い、既存fileを上書きしない。

## 5. Request schema

container clientは単一の`issue_create_request`だけを送信する。payloadはprotocol version、operation、one-time capability、次の内容fieldだけを持つ。

- `title`: 必須の単一行text。
- `summary`: 必須のplain text。
- `context`: 必須のplain text。
- `acceptance_criteria`: 1件以上のplain text配列。

上限はUTF-8 encode後で、`title` 256 bytes、`summary` 2 KiB、`context` 4 KiB、`acceptance_criteria` 1件512 bytes以下かつ1〜20件、protocol frame全体16 KiBとする。NUL、C0／C1 control、bidi override、invalid newline、unknown／missing／duplicate fieldを拒否する。repository、label、assignee、milestone、URL、raw Markdown bodyをrequest fieldとして受け取らない。

host側rendererだけが固定headingとbulletを持つMarkdown bodyへ変換する。previewとGitHub POSTは同じimmutableなcanonical representationを使い、approve時にagent由来の再入力やtemplate差し替えを行わない。

## 6. Intake protocolとruntime

通常のCodex／Claude runtimeを起動するとき、family bindingが有効なprojectだけに専用Unix socketとrun単位のone-time capabilityを渡す。socket directoryとcapabilityは開発GitHub broker、handover broker、egress brokerと分離する。

intake brokerは次を順に検証する。

1. socket peer identityと期待runtime process。
2. protocol frame size、version、operation。
3. run単位capabilityの一致、未使用、期限。
4. exact request schemaと全field制約。
5. project bindingとpending directoryのfilesystem invariant。
6. projectごとの未完了request件数上限10件と、runごとに1件だけの作成制限。

成功時はcryptographically randomなrequest ID、作成時刻、24時間後の期限時刻、canonical contentをprivate fileへ保存し、capabilityを消費する。containerへは固定status `pending`、request ID、期限だけを返す。本文、repository、内部path、credential、GitHub response、詳細なvalidation値は返さない。

broker停止、runtime終了、client disconnectで通常networkや開発GitHub brokerへfallbackしない。stale socket、stale capability、別runからのrequestは拒否する。

## 7. Pending lifecycle

状態は次の固定集合とする。

- `pending`: preview、approve、rejectが可能。
- `sending`: approveがlockを取得し、GitHub requestを開始する直前から保持する内部状態。
- `created`: GitHubの成功responseを完全に検証し、Issue番号とURLをhostで確認済み。
- `rejected`: operatorが明示拒否。
- `expired`: TTL超過。
- `unknown`: GitHub request送信後にtimeout、切断、不正responseなどが発生し、作成有無を証明できない。

`pending`から`sending`を経由するapproveと、`rejected`、`expired`への遷移はlockとatomic state updateで直列化する。`sending`からは成功response検証後の`created`または曖昧結果の`unknown`にだけ移る。approve、reject、expiry cleanupの同時実行を許さず、同じrequestの二重approveを拒否する。process起動時に残っている`sending`は、request送信前だったと推測せず`unknown`へ移す。

成功、reject、expiryではcanonical title/bodyを含むpending contentを削除する。`unknown`ではoperatorが照合できるまでprivate contentを保持するが、通常のapproveと自動retryを禁止する。

## 8. Host CLI

初期interfaceは次を提供する。

```text
agentctl family bind <project> <owner/name>
agentctl family list <project>
agentctl family doctor <project>

agentctl family issue pending <project>
agentctl family issue preview <project> <request-id>
agentctl family issue approve <project> <request-id>
agentctl family issue reject <project> <request-id>
agentctl family issue resolve-created <project> <request-id> <issue-number>
agentctl family issue resolve-not-created <project> <request-id>
```

container imageには限定client `agent-family issue create`だけを含める。host-only commandとcredential pathをcontainerへ含めない。

`pending`はrequest ID、project、作成時刻、期限、状態だけを表示する。`preview`だけがexact repository、canonical title/body、期限をoperatorへ表示する。`approve`は対象、title/body、外部状態への影響を表示し、対話TTYでrequest IDに結び付いた明示確認を要求する。non-interactive bypassや`--yes`は初期scopeに含めない。

## 9. GitHub approval plane

family GitHub AppのpermissionはMetadata readとIssues writeだけとし、Contents、Pull requests、Administrationなどを付与しない。Appは対象family repositoryだけにinstallする。

approveは送信前に次を再検証する。

- requestが`pending`で期限内か。
- previewしたcanonical contentと保存内容が同一か。
- binding fileとpending fileのowner、mode、type、path。
- GitHub App metadata、private key、installation ID。
- installation repository inventoryにexact nameとexact repository IDが1件だけ存在するか。
- installation token permissionがMetadata read、Issues writeの必要十分な集合か。

GitHub requestは固定endpoint `POST /repos/{owner}/{repo}/issues`へ、host-rendered `title`と`body`だけを送る。redirect、別host、別repository、追加response fieldへの追従、generic retryを行わない。成功responseはbounded schemaでIssue番号、expected repository URL、stateだけを検証し、hostにIssue番号とURLだけ表示する。

## 10. Ambiguous outcomeとreconciliation

GitHubへrequest bodyを送る前に確定した失敗は`pending`を維持し、operatorが原因を修正後に再度approveできる。

request送信後に成功responseを証明できない場合は`unknown`へ遷移する。GitHubがIssueを作成した後にresponseだけが失われる可能性があるため、自動retry、通常approve、request複製を禁止する。

operatorはfamily repositoryをhost側の信頼できる経路で確認し、次のどちらかだけを実行する。

- Issueが存在する: exact issue numberを指定して`resolve-created`する。CLIはrepository、title/body、Issue番号を照合してから`created`へ移す。
- Issueが存在しないことを確認した: `resolve-not-created`で`pending`へ戻す。CLIは再送が外部状態を変更し得ることを再表示する。

照合不能な`unknown`は保持し、成功や未作成として推測しない。

## 11. Auditと情報制限

audit eventはtimestamp、project ID、request ID、固定operation、固定status、固定failure stageだけを持つ。title、body、field値、repository名／ID、token、JWT、GitHub raw request／response、URL query、exception textを記録しない。

container outputも固定statusとrequest IDだけに限定する。host previewはoperatorが明示実行した場合だけ本文を表示し、transcriptやhandoverへ自動記録しない。errorは固定messageへ変換し、秘密値やagent payloadをechoしない。

## 12. Failure handlingとlimits

- malformed input、oversize、symlink、owner／mode不正、binding不一致、credential不正はGitHub通信前に拒否する。
- App permissionが過剰または不足する場合も拒否する。
- 未完了requestはprojectごとに最大10件、作成はrunごとに1件、TTLは24時間に固定し、disk exhaustionを防ぐ。
- intake brokerまたはapproval plane失敗時にdirect `gh`、personal token、開発App、通常networkへfallbackしない。
- cleanup失敗は本文を残したまま安全に報告し、成功auditだけを書かない。
- `created`のIssueをrollbackとして自動close／deleteしない。外部状態の修正は別の明示承認操作とする。

## 13. Test strategy

TDDで次を検証する。

### 13.1 Unit

- fixed schema、canonical rendering、UTF-8、control character、size／item limit。
- binding、repository name／ID、App permission、metadata、private key validation。
- secure directory／file、symlink、owner／mode、exclusive create、atomic write、cleanup。
- TTL、rate／count limit、capability、peer identity、stale client。
- lifecycle全遷移、lock、concurrent approve、double approve、reject、expiry。
- GitHub送信前failure、成功response、送信後ambiguous failure、reconciliation。
- auditとerror outputがcontent／credentialを含まないこと。

### 13.2 Integration

- real Unix socketを通るcontainer clientとcredential-free intake broker。
- runtime lifecycle、broker death、socket cleanup、Codex／Claude両経路。
- mock GitHub APIでexact endpoint、body、permission、redirect拒否、bounded responseを確認する。
- rootless Podmanでmount、capability、credential非露出、stale runtime拒否を確認する。
- 開発GitHub broker、handover、egress、project stateの全regressionを通す。

### 13.3 Real-host smoke

専用family test repositoryとfamily専用GitHub Appを使う。Issue作成は外部状態変更なので、実行直前にrepository、canonical title/body、目的、影響を表示し、利用者から個別承認を得る。作成、重複拒否、content-free audit、credential非露出、cleanupを証拠どおり`PASS`、`PARTIAL`、`FAIL`、`not run`で記録する。

## 14. Operationsとrollback

doctorはlocal state、binding、pending invariant、App metadata／permissionの検証結果を固定fieldで表示する。remote repository selectionや実際のGitHub可用性を未観測のまま`PASS`にしない。

rollbackは新規intakeを無効化し、runtimeへfamily socket／capabilityを渡さない。既存pendingはhost operatorがpreviewしてrejectまたは期限切れ処理し、`unknown`はreconciliation完了まで保持する。family App installationを外す前にpending／unknown inventoryを確認する。既に作成済みのIssueを自動変更しない。

## 15. Completion gates

- 開発GitHub brokerとfamily App／state／socket／policyが分離されている。
- containerとintake brokerがGitHub credentialを取得できない。
- agent単独でGitHub外部状態を変更できない。
- exact projectからexactly one family repositoryへのcreateだけが可能である。
- ambiguous outcomeが`unknown`で停止し、重複作成を自動的に起こさない。
- pending contentがterminal state後に削除され、auditがcontent-freeである。
- unit、socket、Podman、実host smokeと全regressionが証拠どおり成功する。
- credential、filesystem、network、external-state、cleanup、rollbackについてCritical／Important findingが残らない独立reviewを完了する。
