# Family Issue作成broker 運用ガイド

この機能は、Codex／Claudeが作った固定schemaのIssue案をhostへ保留し、利用者がrequest単位で確認した場合だけ、登録済みfamily repositoryへIssueを1件作成します。開発用GitHub Appとは別の専用Appを使い、開発brokerへIssue write権限を加えません。comment、edit、close、reopen、delete、label、assigneeは提供しません。作成済みIssueを自動でclose、edit、deleteしません。

## 最初に知っておく境界

- containerへ渡るのはrunごとのsocketとone-time capabilityだけです。App private key、JWT、installation token、repository名／ID、pending host path、approval commandは渡りません。
- requestはhost canonical rendererが`title`と、`## Summary`、`## Context`、`## Acceptance criteria`からなるbodyへ固定します。previewとGitHub POSTは同じ内容を使います。
- requestは24時間で失効し、projectごとの未完了requestは最大10件、1 runにつき作成案は1件です。
- approveは対話TTYから正確に`approve <request-id>`と1行入力したときだけ進みます。`--yes`やnon-interactive bypassはありません。
- bindingがないunbound projectは従来どおり起動し、family socket／capabilityを追加しません。family intakeが失敗しても開発App、`gh`、通常networkへfallbackしません。

## ホスト側Codexへ任せる流れ

container内のCodexとホスト側Codexは直接会話しません。引き継ぎはrepositoryのcommit、文書、必要なら`agent-handover create`で作った最新handoverを介します。ホスト側Codexには次の短い日本語だけを渡してください。

> agent-containerをcheckoutし、最新handoverとdocs/family-issue-create-broker.md、docs/family-issue-create-broker-smoke-test.mdを読んでください。安全なlocal検査と実Podman gateはあなたが実行し、結果を日本語で要約してください。GitHub App設定変更または実Issue作成の直前で停止し、対象・内容・影響を日本語で示して私の承認を待ってください。秘密値は表示・記録しないでください。

ホスト側Codexはclone／checkout、handover読取、前提検査、local test、Podman検証、GitHub App設定画面の案内、公開情報の照合を担当します。長いcommandや英語出力を利用者にcopy-pasteさせないでください。英語の生出力は、`PASS`か停止理由と次の一手へ日本語で要約します。利用者が行うのは、避けられないGitHub UIでの承認、download済みprivate keyの安全な配置、実Issueごとの日本語確認だけです。秘密値はchatやhandoverへ貼らないでください。

## 専用GitHub Appの準備

GitHub UIでfamily専用Appを新規作成します。開発用Appを流用しません。権限とinstallation inventoryは次のexact構成です。

- Repository permissions: `Metadata: Read-only`
- Repository permissions: `Issues: Read and write`
- Contents、Pull requests、Administrationを含む他の権限: `No access`
- Install: `Only select repositories`で選択したfamily repository 1件だけ

ホスト側Codexは設定値を画面と照合できますが、UIの最終保存・installation承認は利用者が行います。AppのClient IDとInstallation IDを、次の固定schemaでhost stateへ保存します。private keyはGitHubからdownloadしたfileをそのまま配置し、内容をterminal、chat、handover、Issueへ表示しません。

```text
$AGENT_CONTAINER_HOME/family/app.json
$AGENT_CONTAINER_HOME/family/private-key.pem
```

```json
{"client_id":"Iv1...","installation_id":12345678}
```

`$AGENT_CONTAINER_HOME`と`family`はcurrent user所有のreal directory、mode `0700`、2 fileはcurrent user所有のregular file、非symlink、single link、mode `0600`にします。値をcommand lineへ埋め込まず、private editorまたはhost側の安全なfile操作を使います。

## Bindingと診断

Appをexact repository 1件へinstallした後、hostだけでbindingを作ります。このcommandはlive installation inventoryが1件だけで、名前とnumeric repository IDが一致する場合だけ保存します。

```bash
bin/agentctl family bind PROJECT OWNER/REPOSITORY
bin/agentctl family list PROJECT
bin/agentctl family doctor PROJECT
```

doctorの表示は次のように解釈します。

- `PASS`: そのcheckが実際に確認できた。local checkのPASSだけでremote権限や実Issue作成まで証明したことにはしない。
- `PARTIAL`: 安全な一部だけ確認済み。残りをPASSへ読み替えない。
- `FAIL`: fail-closedで停止する。修正前にapproveやruntime起動を続けない。
- `not run`: Podman、image、GitHub承認など具体的な前提不足で未実行。PASSではない。

## 通常のoperator workflow

runtime内のagentは`agent-family issue create`でtitle、summary、context、acceptance criterionを送ります。host operatorは本文を一覧へ自動表示せず、次を順番に実行します。

```bash
bin/agentctl family issue pending PROJECT
bin/agentctl family issue preview PROJECT REQUEST_ID
bin/agentctl family issue approve PROJECT REQUEST_ID
```

previewでexact repository、canonical title、canonical body、期限、request IDを確認します。approveは同じ内容とexternal effectを再表示します。正しければprivateな対話terminalへ次の1行だけを入力します。

```text
approve <request-id>
```

rejectする場合は次を使います。期限切れrequestはapproveできず`expired`になります。

```bash
bin/agentctl family issue reject PROJECT REQUEST_ID
```

## `unknown`の照合

送信後にresponseを証明できないrequestは`unknown`です。自動retryや通常approveをしてはいけません。host側の信頼できるGitHub表示でrepositoryとcanonical title/bodyを照合します。

Issueが存在する場合だけ、exact numberを指定します。

```bash
bin/agentctl family issue resolve-created PROJECT REQUEST_ID ISSUE_NUMBER
```

存在しないことを確認できた場合だけ、再送可能な`pending`へ戻します。

```bash
bin/agentctl family issue resolve-not-created PROJECT REQUEST_ID
```

照合不能なら`unknown`のまま保持します。created / not-createdを推測しません。

## Auditと秘密情報

auditはtimestamp、project ID、request ID、固定operation／status／stageだけです。title、body、repository、ID、URL、credential、raw request／response、exception本文を含めません。preview本文をtranscriptやhandoverへ自動転記しません。

## Disable／rollback

1. 新しいruntimeを起動しない。実行中runtimeを通常終了し、socketとcapabilityが消えたことを確認する。
2. pending／unknownを一覧化し、必要ならrejectまたは手動照合する。unknownを推測で解決しない。
3. GitHub UIでfamily専用App installationからrepository accessを外す、またはAppをsuspendする。
4. bindingとcredential stateは証跡・未解決requestの確認が終わるまで保持し、削除する場合もexact pathとbackup方針を別途承認する。
5. rollbackで作成済みIssueを変更しない。close、edit、deleteが必要ならGitHub上の別操作として改めて承認する。

実host検証は[Family Issue broker smoke test](family-issue-create-broker-smoke-test.md)に従います。App／repository provisioningとIssue mutationは自動testに含めません。
