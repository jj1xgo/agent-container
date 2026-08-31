# Family Issue broker 実host smoke test

このchecklistはホスト側Codexが実行し、利用者には日本語の判定だけを返します。GitHub App／repository provisioningとIssue作成は外部状態変更です。事前の包括承認では進めず、各mutation直前に停止します。承認がなければnot runと記録し、PASSへ読み替えないでください。

## Evidence語彙

- `PASS`: commandと観測結果がgateを満たした。
- `PARTIAL`: gateの一部だけを確認した。
- `FAIL`: gate違反を観測した。後続mutationを停止する。
- `not run`: 前提不足または承認なし。理由を具体的に記録する。

credential、repository名／ID、pending本文、approval文字列、capabilityをlogやhandoverへ保存しません。audit確認は件数と固定fieldだけのcontent-free auditにします。

## 1. Local automated gate

checkoutしたcommitを記録し、次をホスト側Codexが実行して日本語で要約します。

```bash
bin/lint
PYTHONPATH=src python3 -m unittest discover -s tests/codex -v
PYTHONPATH=src python3 -m unittest discover -s tests/container -v
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket tests.integration.test_family_intake_socket -v
PYTHONPATH=src python3 -m unittest tests.integration.test_family_forced_unknown -v
git diff --check
```

固定期待値はCodex suiteは`Ran 21 tests ... OK`、container suiteは`Ran 929 tests ... OK`、socket suiteは`Ran 17 tests ... OK`、forced-unknown fixtureは`Ran 4 tests ... OK`です。すべてのcommandでunexpected skipは0件を要求し、件数不一致または1件でも想定外skipがあればPASSにしません。duplicate denial、content-free audit、credential non-exposure、terminal cleanup、forced unknownとcreated / not-created reconciliationがunit／socket testで通ったことをtest名と件数で記録します。秘密値やcanonical本文を記録しません。

## 2. Real Podman gate

これはunit testで代替できない必須gateです。Podman 5.8以降、local rootless Podman、crun、instrumented imageが必要です。remote Podmanや別OCI runtimeではnot runです。

```bash
bin/agentctl --image localhost/agent-family-test:local build
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 AGENT_FAMILY_TEST_IMAGE=localhost/agent-family-test:local PYTHONPATH=src python3 -m unittest tests.integration.test_project_image_podman tests.integration.test_egress_podman tests.integration.test_family_intake_podman -v
```

最初のcommandは必ず検証対象のcheckout直下で実行します。`localhost/agent-family-test:local`はそのcheckoutから構築し、probe commandを実行できる使い捨てinstrumented imageだけに付けるlocal tagです。通常のproduction imageやremote registry imageへ置き換えません。固定期待値はPodman suiteは`Ran 14 tests ... OK`かつunexpected skip 0件です。件数不一致または1件でもskipがあればPASSにせず、Podman、socket許可、crun、imageのmissing prerequisiteごとにnot runと理由を個別記録します。

Family gateではCodex pathとClaude pathの両方について、次を実観測します。

- directory mountではないsingle socket-file bind
- launcher後のpinned fd不在
- preserved directory fdや`openat("..")`によるancestor sentinelへ到達不能
- brokerへ登録したruntime PIDからのactual conmon ancestry
- request 1件成功後のduplicate denial
- environment、argv、mount、inspection、filesystemでのcredential non-exposure
- broker停止時にfallbackせずnonzero、container／socket／markerのterminal cleanup

前提不足はnot runと具体的に記録し、synthetic testのPASSを実Podman PASSへ読み替えないでください。検査用markerのreleaseとthread joinが完了し、残骸がないことも確認します。

## 3. Appとbindingのgate

[運用ガイド](family-issue-create-broker.md)どおり、開発用とは別のApp、`Metadata: Read-only`、`Issues: Read and write`、他はNo access、選択repository 1件だけを確認します。GitHub UIの保存／installation変更直前で対象とexternal effectを日本語表示し、fresh approvalを得ます。未承認ならnot runです。

秘密値を表示せず次を実行します。

```bash
bin/agentctl family bind PROJECT OWNER/REPOSITORY
bin/agentctl family doctor PROJECT
bin/agentctl family list PROJECT
```

doctorはlocal stateとlive inventoryのどちらを証明したか区別し、`PASS`／`PARTIAL`／`FAIL`／`not run`で記録します。

## 4. Credential-free intake

CodexとClaudeを別runで起動し、それぞれ固定fixture案を1件提出します。2件目はduplicate denialであること、container outputが固定status／request ID／expiryだけであることを確認します。containerのenvironment、mount、argv、filesystem、`/proc`にApp metadata、private key、token、repository、pending host path、approval commandがないことを確認します。

hostではpending一覧からrequest IDだけを選び、previewでcanonical titleとcanonical bodyが固定rendererどおりであることを確認します。本文はevidenceへ転記しません。

## 5. STOP: 実Issue作成の直前承認

ここで必ず停止します。ホスト側Codexは次の6項目を日本語で、その場のpreviewから表示します。

```text
exact target repository: <owner/name>
request ID: <request-id>
canonical title: <exact title>
canonical body: <exact body>
purpose: family brokerの実host create-only確認
external effect: GitHubに新しいIssueを1件作成し、自動削除しない
```

「この1件を作成してよい」というfresh explicit approvalが得られたrequestだけ、privateな対話TTYで次を実行します。承認がなければnot runです。

```bash
bin/agentctl family issue approve PROJECT REQUEST_ID
```

promptへは正確に`approve <request-id>`を入力します。成功後はnumberとexpected URLだけを記録し、raw responseや本文を記録しません。同じrequestの再approveが拒否され、作成済みIssueが1件だけであることを確認します。

## 6. Forced unknown／reconcile

実Issueを増やさずに可能な、送信後response喪失を模擬するhost fixtureを使います。`unknown`では通常approveと自動retryが拒否され、private canonical contentが照合まで残ることを確認します。

```bash
PYTHONPATH=src python3 -m unittest tests.integration.test_family_forced_unknown -v
```

このfixtureはhost-only fake provider／creator／inventoryだけを使い、5秒以内に終了します。network、Podman、GitHub、外部状態mutationを使わず、production CLIへforced-unknown bypassを追加しません。固定期待値は`Ran 4 tests ... OK`で、temporary stateはtest終了時にcleanupされます。

- GitHub側に作成済みと確認できるfixtureは`resolve-created`でexact issue number、repository、title/bodyを照合する。
- 未作成を確認できるfixtureだけ`resolve-not-created`でpendingへ戻す。
- 照合不能はunknownのままにする。

forced unknownのために実GitHub POSTを故意に切断してはいけません。追加の実Issue作成が必要になる場合は、そのIssueについてsection 5のfresh approvalへ戻ります。

## 7. Audit／cleanup／rollback

audit JSONLを固定schemaとして検査し、event count、operation、status、stageだけを記録します。title、body、repository名／ID、URL、token、JWT、raw response、exception本文がないことを確認します。runtime終了後はsocket、capability、container、inspection markerが消え、stale clientが拒否され、通常networkや開発brokerへfallbackしないことを確認します。

rollbackは次の順です。

1. runtime停止とterminal cleanup。
2. pending／unknownの安全な解消または保持。
3. Appのrepository access停止。
4. 証跡確認後だけlocal binding／credentialを別途承認して扱う。

rollbackで作成済みIssueを変更しないでください。自動close、edit、deleteは禁止です。

## 結果表

| Gate | Result | Evidence |
|---|---|---|
| Local automated | not run | — |
| Real Podman Codex path | not run | — |
| Real Podman Claude path | not run | — |
| Dedicated App / binding | not run | — |
| Intake / duplicate / non-exposure | not run | — |
| Approved real Issue | not run | — |
| Forced unknown / reconciliation | not run | — |
| Cleanup / rollback | not run | — |
