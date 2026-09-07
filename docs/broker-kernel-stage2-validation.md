# Phase 6 stage 2（S2-4）検証記録

2026-09-07、対象実装はmain `36f02a8`（PR #119 merge）。S2-1〜S2-3が取り込まれていることをlocal Git履歴で確認した。S2-4の今回の変更は文書のみで、Phase 6は進行中。stage 1の結果は[CHANGELOG](../CHANGELOG.md)の当時の記録を参照し、今回のPASSへ転用しない。

## 実行環境

- `/workspace`のsandbox。`command -v podman`はpathを返さず、実host用の実行toolもない。
- 初回の作業branch作成は通常tool sandbox内で`cannot lock ref ... Read-only file system`となった。その後、利用者のcommit依頼を受け、`require_escalated`で`docs/broker-kernel-s2-4`の作成に成功した。container／hostのGit metadata自体がread-onlyという意味ではない。PR作成はnot run。
- 開始時から`.github/workflows/ci.yml`に差分がある。HEADはPodman 17件だが、mountされたworking treeは2 moduleを欠き14件を期待する。既存差分は変更していない。
- required CIの新規実行・remote結果確認はnot run。この記録はlocal検証と実host検証を区別する。

## ローカル検証

結果は下表に実測で記録する。unit／socketの成功は実Podmanや認証済みCLIの成功を意味しない。

| 検証 | 実測結果 |
| --- | --- |
| `bin/lint` | PASS（All checks passed） |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex` | PASS、49件、skip 0 |
| 同じ環境変数で`python3 -m unittest discover -s tests/container` | FAIL、1169件中1件失敗、skip 0。`test_ci_requires_complete_family_podman_gate_without_skips`がworking treeのCIに`test_agent_sandbox_podman`が無いことを検出 |
| `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1`と上記環境変数で`python3 -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket tests.integration.test_family_intake_socket tests.integration.test_family_forced_unknown` | PASS、socket 18件＋forced unknown 4件＝22件、skip 0 |
| HEADの一時コピー＋今回の文書で`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_docs` | PASS、67件、skip 0。HEADのCI定義では上記失敗なし |

一時コピーは`git archive HEAD`を`/tmp/s2-4-verify`へ展開し、今回の文書をコピーして検証した。初回はGit metadataのないarchiveのため`git ls-files`を使う1件がERRORになり、一時コピー内だけで`git init`と文書の`git add`を行って再実行した。元workspaceのGit metadataとCIファイルには書き込んでいない。この67件の成功をcontainer全suite成功やrequired CI成功へ読み替えない。

文書検査: `git diff --check`成功。変更・新規Markdown 6件の相対linkと空白を確認し、CHANGELOGの既存の相対link誤り1件（Claude sandbox設計への`docs/`欠落）を修正した。既存smoke手順書5件はHEADとbyte一致。

## 前セッション時点の実host smoke（すべてnot run）

理由: このsandboxにはPodmanと実host実行手段がない。認証やfixtureの現在状態も未確認。以下の既存手順書は変更していない。

| 手順書 | 再実行するgate | 今回の結果 |
| --- | --- | --- |
| [Phase 2](phase2-smoke-test.md) | 現行方式のClaude sandbox、認証、編集・test・resume、non-exposure、handover・cleanup（historical diagnosisは再実行対象外） | not run |
| [Phase 3](phase3-github-broker-smoke-test.md) | inventory／doctor、non-exposure、clone／fetch、create-only push、PR、Issue read、audit／cleanup | not run |
| [Phase 4](phase4-stabilization-smoke-test.md) | 現行private fixture gate、create-only拒否、Issue read、stale client、cleanup、自動検証・review（過去のrelease公開操作は再実行しない） | not run |
| [Egress](egress-domain-allowlist-smoke-test.md) | 手順書のallowlist／denial、実Codex／Claude、audit、cleanup／rollback | not run |
| [Family](family-issue-create-broker-smoke-test.md) | 実Podman 17件、App／binding、両CLI intake、non-exposure、duplicate、承認付きIssue、unknown／reconcile、audit／cleanup／rollback | not run |

GitHubはSameUserによる実clientの許可とjoin後失効を確認し、30秒client timeoutが実操作へ与える影響を記録する。egressは現在のcapability mode `0600`でadapterが起動することも既存手順中で確認する。

## 実hostでの続行

1. 対象checkoutのcommitと差分を再確認し、required CIのunit・socket 3 module・Podman 17件を照合する。現在のworking treeの古いCIを基準にしない。
2. 各手順書を読み、rootless Podman／crun、認証済みCLI、専用project、fixture、App／bindingを確認する。build対象commit、image ID、CLI version、実施日時を記録する。
3. 上表の現行gateを既存command・assertionのまま実行する。GitHub mutation、Family実Issue作成、rollbackは各手順書の対象と承認条件を満たしてから実施する。秘密値や本文は記録しない。
4. gateごとにPASS／FAIL／not runと理由・bounded evidenceを追記する。cleanupとrollbackを分け、未実施のrollbackを成功扱いしない。再実行に必要な承認は具体的な対象が揃ってから受ける。
5. 必要な実host gateとCIがPASSし、文書・証拠がmainへ取り込まれた後にPhase 6を閉じる。未実施項目が残る間は進行中を維持する。

## 次の実装

利用者指定（2026-09-07）: S2-4の実host smoke・required CI・必要なmain取り込みを完了した直後、Phase 7へ進む前に[Issue #120: Codex handoverのcreate-only broker統一](https://github.com/jj1xgo/agent-container/issues/120)へ最優先で着手する。S2-4へruntime変更を混ぜず、独立した設計・実装PRとする。

## 実hostでの再開（2026-09-07）

前セッションの専用workspaceでbranch `docs/broker-kernel-s2-4`、commit `5a2a49a`を確認し、ホストrepositoryへ取得して`.worktrees/broker-kernel-s2-4`に同commitのworktreeを作成した。元workspaceのCI差分と未追跡handoverは保持している。ホストのmainは`e41d096`のまま、fetch後の`origin/main`は`36f02a8`。このworktreeのCIはHEADと一致し、Podman gateは17件である。

### Buildとlocal gate

- local Podman `5.8.6`、rootless `true`、OCI runtime `crun`。
- 対象checkoutで`bin/agentctl --image localhost/agent-family-test:local build`がexit 0。version固定optionなし。
- image ID: `e9791cbc483f8516963d3afe85da6c662373156b77e5904ad4868b3a98e12c02`。
- 公開version: Node `v26.8.1`、Codex `0.153.4`、Claude `2.1.263`。image内agentctlは`0.6.0-dev.46+g5a2a49a`、`agent-github --help`もexit 0。

| 検証 | 今回の実測結果 |
| --- | --- |
| `bin/lint` | PASS |
| Codex unit | PASS、49件、skip 0 |
| container unit | PASS、1169件、skip 0 |
| socket 4 module＋forced unknown | PASS、18＋4＝22件、skip 0、ResourceWarningなし |
| 実Podman 5 module | PASS、17件、138.403秒、skip 0。専用imageをbase imageとFamily imageの双方に指定し、socket／Podman integrationを有効化。両agentのFamily ancestry／duplicate／non-exposure／cleanup、egress、strong nested sandbox、Codex sandbox内socket接続を含む |
| 専用smoke projectのdoctor（両agent、GitHub broker、専用image） | PASS、全項目成功。Claude auth status、managed policy、handover client、既存egress policyも成功。実推論・remote inventoryの成功とは区別する |
| Family doctor | local state、binding、pending invariants、audit、App metadata permissionsはPASS。remote availabilityはnot run |

上記unit／socketのcommandは既存gateと同じで、`PYTHONDONTWRITEBYTECODE=1`を追加した。初回の通常sandboxではcontainer suiteのsocket関連31件がERROR、1件skip、managed policy 1件FAIL、socket suiteは18件ERRORだった。socket作成は`EPERM`であり、sandbox外で再実行した。managed policy失敗は新規checkoutの`profiles/claude/statusline.sh`が`0664`で、validatorが`0644`を要求したため。`CLAUDE.md`と同fileのworktree内modeを`0644`へ合わせ、対象test成功後にホストでcontainer全suiteを実行して上表の結果を得た。追跡fileの内容やvalidatorは変更していない。buildとdoctorも通常sandboxではPodmanを利用できず、ホスト実行へ切り替えた。

### CIと残存gate

[PR #119](https://github.com/jj1xgo/agent-container/pull/119)はmerge済みで、head `91585c6bd89cfdfbaa27576d498db7fad5894eca`、merge commit `36f02a87c14603114bd5856575c0c92b49a07d66`を照合した。[同PRのCI](https://github.com/jj1xgo/agent-container/actions/runs/34072250396)はUnit tests／Podman integrationともpass。これは先行PRの結果であり、S2-4 commitの新規required CIはnot run（push／PR未実施）。

Codexの最小応答とegress cleanup、Claudeの最小応答とcredential非露出probeは下記のとおり確認した。GitHub clone／fetchとcreate-only push／PR、Issue read／stale client、Family両CLI intake／実Issue作成、各rollbackはnot run。専用smoke projectの既存workspaceは追跡branchに対してahead 2／behind 1であり、resetや既存branch変更はしていない。次の実サービスgateは対象project・agent・exact domain・操作を具体化し、各手順書のfresh approval条件を満たしてから行う。Phase 6は引き続き進行中。

今回のlocalログ: `/tmp/s2-4-host-{build,codex,container,socket,podman,doctor-smoke,docs}.log`。credential本文を取得せず、認証関連の直接観測は既存手順で許可されたmetadataとdoctor結果に限定した。

### 承認済みCodex runtime gate（2026-09-07）

利用者が専用`agent-container-smoke`でのCodex起動1回、最小応答、終了後cleanupを直前承認した。既存のexact domainは`chatgpt.com`、`ab.chatgpt.com`、`pypi.org`、`sdmntprsouthcentralus.oaiusercontent.com`であり、policyを変更していない。

host checkout `c27563a`（runtime実装は`5a2a49a`と同一）と上記imageを使用。`agentctl.main`の通常run・egress supervision経路を使い、一時driver `/tmp/s2-4-codex-runtime-smoke.py`のruntime builderでPodmanの対話用flagsを外し、標準Codex commandへ`exec --ephemeral --json --color never`とtool使用・file変更をしない最小promptを付加した。mount、network、credential、broker、監視処理は標準builderのまま。prompt／response、stderrの生本文はevidenceへ保存せず、子process出力はメモリ内で判定して破棄した。

| 観測 | 結果 |
| --- | --- |
| 認証済み最小応答 | PASS、期待応答一致、turn completed、tool使用なし、launcher／processともexit 0、timeoutなし。起動は1回だけ |
| Egress | PASS、runtime specは`--network=none`、capability file modeは`0600`、既存allowlist不変 |
| Content-free audit | 固定schema検査PASS。このproject／agentの追加分は`connect`成功16件、`denied`／`policy` 1件。拒否domainは収集せず、必要な追加domainがあるかは未判定。最小応答成功をCLI全機能成功へ読み替えない |
| Cleanup | PASS、通常終了後に対象container、socket、capability、run directoryがすべて不在 |
| Workspace | 実行前後のGit status（branchを含む）が一致 |

この実行ではgateway故障時のfallback、stale client、rollbackは再検証していない。先行する17件の実Podman統合testの結果と区別し、Egress手順全体とS2-4全体はPARTIALのまま維持する。

### 承認済みClaude runtime gate（2026-09-07）

利用者が専用`agent-container-claude-smoke`でのClaude起動1回、最小応答、sandbox内のcredential非露出probe、終了後cleanupを直前承認した。このprojectはdomain制限なしであることを提示済み。事前doctorは必須checkすべてPASS、network-policyだけ既知WARN。

host checkout `b127e14`（runtime実装は`5a2a49a`と同一）と上記imageを使用。一時driver `/tmp/s2-4-claude-runtime-smoke.py`から通常`agentctl.main run`経路を呼び、標準Claude runtime builderのPodman対話flagsを外した。Claude commandには`--print --verbose --output-format stream-json --no-session-persistence`、`--permission-mode dontAsk`、`--tools Bash`、既存probe commandだけの`--allowedTools`を付加した。通常のmanaged policy、credential launcher、mount、network、handover broker、container境界を維持し、sandbox無効化やbypassは使用していない。生の応答・stderrはメモリ内で判定して破棄し、本文をevidenceへ保存していない。

| 観測 | 結果 |
| --- | --- |
| 認証済み最小応答 | PASS、期待応答一致、result success、launcher／processともexit 0、timeoutなし。起動は1回だけ |
| Bash probe | PASS、`python3 -m agent_container.claude_security_probe`を1回だけ実行。実tool resultが期待する3行だけで、`oauth_token_visible=false`、`token_file_readable=false`、`parent_token_via_proc_readable=false`。sandbox無効化指定なし、追加tool呼び出しなし |
| MCP | init eventのMCP server一覧は空 |
| Container境界 | 標準specのread-only、cap-drop all、no-new-privilegesを確認 |
| Cleanup | PASS、通常終了後に対象project／agentのcontainer、handover brokerのsocket・capability・run directoryがすべて不在 |
| Workspace／project credential | Git status（branchを含む）は実行前後で一致、project `.credentials.json`は実行前後とも不在 |

`/status`、`/sandbox` Config、`/hooks`の対話画面、`/proc`のprocess数照合、編集・test・local commit・resume、handover create／stale client、他projectとの分離はこのrunではnot run。managed policyの事前doctor成功と上記probe成功を、それらの手動gate成功へ読み替えない。Claude runtimeの今回承認範囲はPASS、Phase 2手順全体とS2-4全体はPARTIALのまま維持する。

### 承認済みGitHub read gateの試行（2026-09-07）

利用者が専用`agent-container-smoke`のGitHub broker経由での`git fetch origin`、`agent-github issue list`、Issue #1／#2のview、終了後cleanupを承認した。host checkout `dbf018c`と上記imageから、通常のCodex runtime builderと`--github-broker`、既存egress policyを使い、4commandを順に1回ずつ実行するよう非対話Codexへ依頼した。一時driverは`/tmp/s2-4-github-read-smoke.py`。

結果は**操作gate not run／driver検証FAIL**。runtimeは16.923秒でexit 0、turn completedだったが、`command_execution`完了eventは0件で、対象projectのGitHub broker audit追加も0件。fetch／Issue読み取りの成功証拠は得られていない。応答・stderrの生本文は保存せず判定後に破棄したため、未実行の理由は判定不能。GitHub通信の失敗やclient timeoutを観測したとは扱わない。

CleanupはPASS。GitHub／egress双方のsocket・capability・run directory、対象containerがすべて不在。既存egress policy、fixture manifest、workspace fileのGit状態、local HEADは不変。runtime specは`--network=none`でlegacy host gh mountなし。

追加の外部試行は未実施。次の候補として、同じPodman mount・network-none・GitHub brokerとhost側監視を使い、container内の実`git`／`agent-github`を直接実行する一時driver `/tmp/s2-4-github-direct-read-smoke.py`を準備し、構文検査だけ行った。この候補はCodexのtool実行を検証するものではなく、実clientとbrokerのgateとして別に記録する。Codex／egress agent commandはPythonの固定4commandに置き換えるが、外向きnetworkを追加しない。実行はfresh approval待ち。
