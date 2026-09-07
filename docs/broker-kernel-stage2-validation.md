# Phase 6 stage 2（S2-4）検証記録

2026-09-07、対象実装はmain `36f02a8`（PR #119 merge）。S2-1〜S2-3が取り込まれていることをlocal Git履歴で確認した。S2-4の今回の変更は文書のみで、Phase 6は進行中。stage 1の結果は[CHANGELOG](../CHANGELOG.md)の当時の記録を参照し、今回のPASSへ転用しない。

## 現在地一覧（2026-09-07、handover照合後）

後続sectionは時系列の追記であり、判定の現在値は本表が正である。担当交代時のhandover（`2026-09-07_083807`）の指示に従い、既存smoke手順5件と[stage 2設計](superpowers/specs/2026-09-06-broker-kernel-stage2-design.md)の必須項目へ、これまでの証拠を対応付けた。追加の実サービス操作は行っていない。

共通の対象版: host checkout `5a2a49a`〜`d46d51a`（runtime実装はmain `36f02a8`と同一、後続commitは文書のみ）、専用image `e9791cbc483f`、Codex `0.153.4`、Claude `2.1.263`、rootless Podman `5.8.6`／crun。stage 1（6-6）の証拠は2026-09-05〜06、image `edd9916b52f3`、Claude `2.1.261`、旧broker実装であり、本表では「stage 1証拠」と明記して区別する。

証拠種別: `自動test`（unit／socket／実Podman suite）、`直接CLI`（通常runtime経路のmount・network・brokerを保ちagent commandだけを固定commandへ置換）、`guard付き`（直接CLIに加えテストprocess内で送信直前を止めるguard）、`実agent`（認証済みCodex／Claudeのtool実行）、`手動TUI`（利用者のprivate terminal）。stage 2が観測挙動を変える項目（K1〜K5、H1〜H4、E1〜E4、G1〜G8、F1〜F2）は「stage 2対象」と記す。

| 手順書 | 必須項目 | 証拠種別 | 採用証拠と対象版 | 判定 | 不足・次の確認 |
| --- | --- | --- | --- | --- | --- |
| Phase 2 preflight | 全unit、`git diff --check`、derived image実Podman | 自動test | 2026-09-07 host: lint、Codex 49、container 1169、socket 18＋forced unknown 4、実Podman 17（`test_project_image_podman`含む）、skip 0 | PASS | — |
| Phase 2 §2 | clean projectのdoctorと最小inference | 実agent（非対話`--print`） | `agent-container-claude-smoke`のdoctor PASS、最小応答PASS | PASS | 対話TUI経路の起動は未実施 |
| Phase 2 §3 | `/status`、`/sandbox` Config、`/hooks`、`/mcp` | 手動TUI | 2026-09-07のhandover create runで利用者が確認。`/status`は正常表示、`/sandbox`はsession記録上`Error: Sandbox settings are overridden by a higher-priority configuration and cannot be changed locally.`（stage 1と同じmanaged強制の表示、sandbox_instructions attachmentあり）、`/hooks`はstage 1と同じ（user scope pluginのhook 1件がpolicyでblock済み）、`/mcp`は0件 | PASS | stage 2はClaude sandbox設定を変えない |
| Phase 2 §4 | security probe 3項目false | 実agent（Bash tool） | S2-4 PASS: 3項目false、追加tool呼び出しなし | PASS | — |
| Phase 2 §4 | `/proc` process数照合 | 実agent（手動TUI） | 2026-09-07のhandover create runで`ls /proc \| grep -c '^[0-9]\+$'`を1回実行し出力は`7`。agentは内訳を述べず、sandbox内process数との照合は未実施。`test_agent_sandbox_podman`（自動test）で`/proc` unmask後のsandbox実行を確認 | PARTIAL | 親Claude PIDを含まないことの照合はstage 1でも未実施。stage 1と同じ範囲で受け入れ |
| Phase 2 §5〜6 | quarantined credentialの回復 | — | 2026-08-26に1回限り実施済み | N/A | 再実行対象外 |
| Phase 2 §7 | 編集・focused test・local commit・resume | 実agent（TUI） | S2-4 not run。stage 1（6-6、2026-09-06）でも未実施、2026-08-26のみPASS | not run（stage 1証拠を採用、利用者判断2026-09-07） | stage 2対象外 |
| Phase 2 §8 | project `.credentials.json`不在、他projectからの非観測 | metadata | S2-4: project `.credentials.json`は実行前後とも不在PASS。他projectからの観測不能はnot run | PARTIAL（stage 1証拠を採用、利用者判断2026-09-07） | stage 2対象外 |
| Phase 2 §9 | dual-agent doctor、Codex回帰 | 直接CLI／実agent | doctor両agent PASS、Codex最小応答PASS、全suite PASS | PASS | — |
| Phase 2 handover gate 1 | Claude handover create（7 section、path-only stdout、audit `create`／`ok`／`write` 1行） | 実agent（手動TUI） | **S2-4 PASS**（2026-09-07、下記「承認済みClaude handover create gate」）。利用者のprivate terminalから起動した実ClaudeのBash toolが`agent-handover create`を1回実行し、host fileのbyte一致、audit 1行、cleanupを確認 | PASS（stage 2対象H1〜H4） | Codex handoverはbrokerを使わないdirect writerのためstage 2 gateにならない（Issue #120） |
| Phase 2 handover gate 2〜4 | read-only拒否、cross-project拒否、malformed／secret拒否 | 自動test（実hostは2026-08-27のみ） | S2-1後の`test_handover_broker_socket`等PASS。stage 1（6-6）でも実host再実施なし | PARTIAL（自動test） | stage 1と同じ範囲で受け入れ |
| Phase 2 handover gate 5 | non-logging（stdout／audit） | 実agent／直接CLI | create runのstdoutは作成pathのみ、audit追加行にtitle・本文sentinelなし | PASS | — |
| Phase 2 handover gate 6 | 終了後のsocket／capability消滅、stale client拒否 | 直接CLI | run directory・socket・capability不在、旧pathを使うstale clientはexit 1・stdout空・固定stderr・audit不変・新規fileなし | PASS | — |
| Phase 3 §1 | rootless Podman、App installation／permission | host read-only | rootless PASS。開発用App installation／permissionはS2-4未再確認（2026-08-26／29の記録） | PARTIAL | stage 2対象外、採用 |
| Phase 3 §2 | broker doctor | 直接CLI | 両agent＋`--github-broker` PASS | PASS | — |
| Phase 3 §3 | runtime credential非露出（env、mount、argv、`/proc`、`gh auth status`） | spec検査 | S2-4はruntime specの`--network=none`とlegacy gh mount不在のみ。container内のenv／argv／`/proc`検査と`gh auth status`はnot run。stage 1証拠: 2026-09-05 PASS | PARTIAL（stage 1証拠を採用、利用者判断2026-09-07） | stage 1より狭いが、stage 2はmount・環境の受け渡しを変えない |
| Phase 3 §4／Phase 4 §4 | clone／fetch、別repository拒否、broker停止後の失敗 | 直接CLI | clone（保存origin HTTPS、実効origin broker URL）／fetch PASS、別repository read拒否PASS、終了後stale client（issue view）PASS | PASS | fetchによるstaleは未実施、issue viewで代替 |
| Phase 3 §5／Phase 4 §4 | create-only push、既存branch FF／NFF・protected・delete・tag拒否 | 直接CLI／guard付き | push（新規branch、空commit）PASS。negative 5件はguard到達0で拒否、remote refs不変。stage 1も同じguard方式 | PASS | — |
| Phase 3 §6／Phase 4 §4 | PR create／view／checks、merge・close・generic不在 | 直接CLI／自動test | smoke PR #5 PASS。不在interfaceはunit test | PASS | smoke PRはOPENのまま保持 |
| Phase 3 §7／Phase 4 §5 | Issue list／view、write・query拒否、stale | 直接CLI／自動test | list／view PASS、stale PASS。write／query拒否は2026-08-29の実host＋unit test | PASS | — |
| Phase 3 §8／Phase 4 §6 | audit／cleanup | 直接CLI | 固定schema、stageなし、artifact不在 PASS | PASS | — |
| Phase 3／4 補足 | 実agent経由のbroker操作 | 実agent | Codex非対話1回はtool 0件で非診断。stage 1（6-6）も直接CLI方式 | not run（stage 1と同方式） | agentはbroker境界の外側のため必須にしない |
| Phase 4 §7 | 自動検証と独立review | 自動test | 全suite PASS。独立reviewはS2-4 PRで | PARTIAL | PR時に実施 |
| Phase 4 §8 | release gate | — | v0.4.0専用 | N/A | S2-4対象外 |
| Egress §1 | local preflight、doctor | 直接CLI | 既存policy（4 domain）保持、doctor PASS | PASS | — |
| Egress §2 | discovery | — | 既存allowlistで最小応答成功のため不要 | N/A | — |
| Egress §3 | Codex approved runtime、capability `0600`、adapter起動、cleanup | 実agent | PASS: connect ok 16、policy denied 1、cleanup | PASS（stage 2対象E1〜E4） | — |
| Egress §3 | Claude egress | 実agent | not run。Claude smoke projectはdomain制限なし、stage 1でもnot run | not run（stage 1と同じ） | 採用 |
| Egress §4 | remove／rollback | — | not run、policy保持 | not run（stage 1と同じ） | 記録のみ |
| Egress 補足 | gateway故障時fallbackなし | 自動test | `test_egress_podman`のgateway death PASS | PASS（自動test） | — |
| Family §1 | local automated | 自動test | PASS（件数は上記） | PASS | — |
| Family §2 | real Podman 17 | 自動test | PASS、skip 0 | PASS（stage 2対象F1〜F2） | — |
| Family §3 | App／binding | host | doctor＋live inventory PASS、変更なし | PASS | — |
| Family §4 | Codex／Claude intake、duplicate、non-exposure | 実agent＋自動test | Codex PASS、Claude PASS（v4 driver）。non-exposureは実Podman suite | PASS | Claude非対話のtool 0件履歴は保持 |
| Family §5 | 承認付き実Issue、再approve拒否 | 手動CLI | Issue #121 PASS、再approve拒否 | PASS | — |
| Family §6 | forced unknown | 自動test | PASS 4件 | PASS | — |
| Family §7 | audit／cleanup／rollback | 直接CLI | cleanup PASS。rollbackはnot run（binding保持、stage 1と同じ） | PASS／rollback not run | Codex由来pending 1件は未送信、期限を確認して無断で送信／rejectしない |
| stage 2受け入れ | required CI（unit、socket 3 module、Podman 17） | CI | [PR #122](https://github.com/jj1xgo/agent-container/pull/122)の`34829c6`で[Unit tests／Podman integration](https://github.com/jj1xgo/agent-container/actions/runs/34103416701)ともpass | PASS | roadmap更新commitの再実行結果はPRで確認 |
| stage 2受け入れ | main取り込み後にPhase 6を閉じる | — | PR #122でroadmapのPhase 6を完了へ変更。merge後に確定 | PR待ち | merge後、Issue #120へ |

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

## 初回sandbox時点の実host smoke（歴史記録、当時すべてnot run）

以下は初回の通常sandboxで作業した時点の記録で、現在の判定は冒頭の現在地一覧が正である。理由: 当時のsandboxにはPodmanと実host実行手段がなかった。認証やfixtureの現在状態も未確認。以下の既存手順書は変更していない。

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

Codexの最小応答とegress cleanup、Claudeの最小応答とcredential非露出probe、直接CLIによるGitHub clone／fetch／Issue read／新規branch作成push／PR create・view・checks、終了後stale client拒否、guard付きnegative 6件は下記のとおり確認した。cloneのorigin検査はdriver修正後の直前承認付き再実行でPASS。Family両CLI intake／実Issue作成、各rollbackはnot run。専用smoke projectの既存workspaceは追跡branchに対してahead 2／behind 1であり、resetや既存branch変更はしていない。次の実サービスgateは対象project・agent・exact domain・操作を具体化し、各手順書のfresh approval条件を満たしてから行う。Phase 6は引き続き進行中。

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

この時点では追加の外部試行は未実施。次の候補として、同じPodman mount・network-none・GitHub brokerとhost側監視を使い、container内の実`git`／`agent-github`を直接実行する一時driver `/tmp/s2-4-github-direct-read-smoke.py`を準備し、構文検査だけ行った。この候補はCodexのtool実行を検証するものではなく、実clientとbrokerのgateとして別に記録する。Codex／egress agent commandはPythonの固定4commandに置き換えるが、外向きnetworkを追加しない。直前承認後の結果を次節に記録する。

### 直接CLIによるGitHub read gate（2026-09-07）

利用者のfresh approval後、host checkout `0768370`と上記imageから準備済みの直接CLI driverを1回実行した。通常`agentctl.main run --github-broker`、標準mountと`--network=none`、GitHub／egress brokerの生成・監視・停止は維持し、container commandのみ固定4操作を順番に実行するPythonへ置換。Codexとegress adapter自体は起動していない。この結果をCodex sandbox内tool実行やegress adapter成功へ読み替えない。

| 実CLI command | exit | 実測秒数 | 判定 |
| --- | --- | --- | --- |
| `git fetch origin` | 0 | 1.513 | PASS、`git-upload-pack` audit 1件ok |
| `agent-github issue list` | 0 | 0.798 | PASS、固定schema、open Issue #1あり、closed Issue #2とPR #3は不在、stderr空 |
| `agent-github issue view 1` | 0 | 0.512 | PASS、固定schema、open state、exact URL、期待body sentinel、stderr空 |
| `agent-github issue view 2` | 0 | 0.497 | PASS、固定schema、closed state、exact URL、期待body sentinel、stderr空 |

全体5.364秒、launcher／processともexit 0、timeoutなし。GitHub brokerのSameUser peer policyは実container clientの4操作を許可し、30秒client timeoutの影響はこの4操作では観測されなかった。audit追加4件は`git-upload-pack` 1件、`issue-list` 1件、`issue-view` 2件で、すべて`ok`、stageなし、固定schema。stdout／driver stderrにexcluded-field sentinelとPR sentinelは不在。fetchのstderrは非空で、本文を保存していないため内容の分類は行っていない。Issue本文を含むCLI出力はメモリ内だけで照合して破棄した。

Cleanup PASS: GitHub／egress双方のsocket・capability・run directoryと対象containerが不在。legacy host gh mountなし。egress policy、fixture manifest、workspaceの追跡／未追跡file状態、local HEADは不変（fetchによるremote-tracking ref／FETCH_HEAD更新は許可操作）。

### 次のcreate-only pushのローカル準備

専用smoke repositoryの既存workspaceとbranchを保持し、`origin/main`の`98ecc7c`から`/tmp/s2-4-github-push-fixture`へworktreeを作成した。新規branchは`test/github-broker-smoke-s2-4-20260907`、空commitは`99a8a51`（`test: GitHub broker S2-4 smoke`）。親commitとのfile差分は0。この準備段階ではremoteへ未送信だった。

初回pushを直前承認後に実行した結果は次節のとおり。PR作成、negative push、stale client、rollbackは引き続きnot runで、それぞれ既存手順の対象・承認条件を満たしてから実施する。S2-4は未完了。

### 承認済み新規branch作成push（2026-09-07）

利用者がexact repository `jj1xgo/agent-container-smoke`、新規branch `test/github-broker-smoke-s2-4-20260907`、空commit `99a8a51`の初回作成pushを直前承認した。host checkout `8e11f12`と上記image、一時driver `/tmp/s2-4-github-push-smoke.py`で、前節と同じ直接CLI／通常broker監視経路を使用した。

実行前にlocal refが`99a8a51e629f0b5fd0a65931b93bae6d1b9b8d84`と一致すること、broker経由の`git ls-remote --refs origin`で新規branchが不在であることを確認し、次のpushを1回だけ実行した。

```bash
git push --porcelain origin refs/heads/test/github-broker-smoke-s2-4-20260907:refs/heads/test/github-broker-smoke-s2-4-20260907
```

PASS: exit 0、1.791秒、`new branch`報告あり。push後のbroker経由のref照合で、remoteの新規branchがexact commitと一致し、全ref集合の変化はこのbranch追加だけだった。auditは`git-receive-pack` 1件ok（承認ref一致）と前後照合の`git-upload-pack` 2件ok、stageなし、固定schema。30秒client timeoutによる失敗は観測されなかった。

launcher／processともexit 0、timeoutなし。GitHub／egress双方のsocket・capability・run directoryと対象containerが不在、既存workspaceのfile状態とlocal HEADは不変。`--network=none`とlegacy host gh mountなしを維持した。create-onlyの正の操作はPASSだが、既存branch更新拒否などのnegative gateはまだnot run。

準備したPR候補: repositoryは同じ専用smoke repository、base `main`、headは上記branch、titleは`test: GitHub broker S2-4 smoke`、bodyは「Phase 6 S2-4の承認済みsmoke test。空commitのみでファイル変更なし。mergeしない。」。本文を`/tmp/s2-4-smoke-pr-body.md`に準備し、別のfresh approval後に次節のとおり作成した。mergeや自動close／deleteは行わない。

### 承認済みsmoke PR create／view／checks（2026-09-07）

利用者のfresh approval後、host checkout `29505f7`と上記image、一時driver `/tmp/s2-4-github-pr-smoke.py`を使い、同じ直接CLI／通常broker監視経路からPRを1件作成した。[専用smoke PR #5](https://github.com/jj1xgo/agent-container-smoke/pull/5)はOPEN、base `main`、head `test/github-broker-smoke-s2-4-20260907`、head SHA `99a8a51e629f0b5fd0a65931b93bae6d1b9b8d84`。host側のread-only `gh pr view`でもbase／head／SHAと承認済みtitle／bodyの一致を確認した。PR createの呼び出しは1回だけ。

| 実CLI操作 | exit | 実測秒数 | 結果 |
| --- | --- | --- | --- |
| `agent-github pr create` | 0 | 2.376 | PASS、固定schema、期待title／state／URL |
| `agent-github pr view 5` | 0 | 0.580 | PASS、create結果と一致 |
| `agent-github pr checks 5` | 0 | 1.099 | PASS、固定schema、checks 0件。読み取り成功でありCI成功ではない |

3操作ともstderr空。auditは`pr-create`／`pr-view`／`pr-checks`各1件ok、stageなし、固定schema、createのPR番号一致。launcher／processともexit 0、timeoutなし。GitHub／egress双方のsocket・capability・run directoryと対象containerが不在で、既存workspaceのfile状態とHEADは不変。PRはmergeせずOPENのまま保持する。S2-4文書branch自体のrequired CI／PR／main取り込みとは別のsmoke結果である。

次のread-only gate候補として`/tmp/s2-4-github-clone-stale-smoke.py`を準備し、構文検査を実施した。専用repositoryをcontainer内`/tmp`の一意な一時directoryへ新規cloneし、exact originとmain commitを照合して一時cloneを除去する。runtime中にsocket／capabilityのpathだけを保持し、通常終了後にその設定の実clientが固定errorで拒否されることとaudit不変を確認する。capability本文は取得・複製しない。直前承認後の結果は次節のとおり。

### 承認済み新規clone／stale client gate（2026-09-07）

利用者のfresh approval後、host checkout `1a387d6`と上記image、準備済みdriverを1回実行した。同じ直接CLI／通常broker監視経路から、containerの一意な一時directoryへ`git clone https://github.com/jj1xgo/agent-container-smoke.git`を実行。

| 観測 | 結果 |
| --- | --- |
| clone command／main | PASS、exit 0、1.735秒、clone先HEADは期待main `98ecc7c45892c62ba9e541991bd39cd99a4cf1c5`と一致 |
| origin検査 | driver FAIL。`git remote get-url origin`を入力HTTPS URLと比較したassertionがfalse。実URLは出力せずメモリ内で破棄済みのため、この実行のorigin検査は未確定 |
| 一時clone除去 | PASS、container内の一時directoryは不在 |
| audit | `git-upload-pack` 1件ok、stageなし、固定schema |
| 終了後stale client | PASS、保持した旧socket／capability pathを使う実clientのIssue viewがexit 1、stdout空、stderrは固定`error: GitHub broker request failed`、audit不変。socket消失後の拒否であり、稼働中brokerへの失効済みcapability提示とは区別する |
| Cleanup／workspace | PASS、GitHub／egress双方のsocket・capability・run directoryと対象containerが不在。既存workspaceのfile状態とHEADは不変 |

origin assertionによりdriver／launcherはexit 1、timeoutなし。clone通信失敗やbroker拒否ではない。外部操作を再実行せず、`_broker_git_args`のexact `url.agent-broker://...insteadOf=https://...git`設定を確認した。外部通信なしの一時Git repositoryで同じ書き換えを再現し、`git remote get-url origin`は実効broker URLを返すため旧HTTPS assertionが失敗し、`git config --get remote.origin.url`は保存されたHTTPS URLを返すことを確認した。

一時driverの比較だけを修正し、`/tmp/s2-4-github-clone-stale-smoke-v2.py`を準備した。保存HTTPS URLと実効broker URLを別々に照合する。driverとcontainer probeの構文検査、外部通信なしの旧assertion失敗／新assertion成功を確認した。runtime実装・既存smoke手順・sandbox境界は変更していない。修正driverの実行前は新規clone gateをPARTIALとして保持し、直前承認後の結果を次節へ記録した。

### 修正driverによるclone／stale client再検証（2026-09-07）

利用者のfresh approval後、host checkout `5a8d3c7`と同じimageからv2 driverを1回実行。**PASS**: clone exit 0、1.423秒、期待main commit一致、保存originはexact HTTPS URL、実効originはexact `agent-broker://jj1xgo/agent-container-smoke`と一致。一時cloneは除去済み。auditは`git-upload-pack` 1件ok、stageなし、固定schema。

runtime終了後の旧path設定を使ったstale clientはexit 1、stdout空、固定stderr、audit不変。GitHub／egress双方のsocket・capability・run directoryと対象containerは不在。launcher／processともexit 0、timeoutなし、既存workspaceのfile状態とHEADは不変。初回driver失敗の履歴は保持し、今回の成功を再実行の結果として区別する。

### 次のcreate-only拒否gateの準備

既存workspaceを保持し、専用smoke repositoryの`99a8a51`から`/tmp/s2-4-github-negative-fixture`にlocal branch `test/github-broker-smoke-s2-4-20260907-ff`を作成。空commit `3d5a45d95df889d44da9db5a8ced11daf7d96fd1`をFF更新候補とした。remoteへ未送信。

一時driver `/tmp/s2-4-github-negative-smoke.py`で次の6件を各1回検証する準備をした。通常の実container CLIとbroker policyを使い、receive-packのGitHub POSTを行う`GitHubReceivePackTransport.rpc`だけにテスト用の送信停止guardを設ける。正しい拒否ではこのguardの呼び出しが0件であることを要求し、1件でも到達した場合はFAILとする。repository sourceは変更せず、検査process内だけの差し替え。guardの送信前停止と復元を、外部通信なしのlocal checkで確認した。実hostの拒否gateはまだnot run。

- 専用smoke branchのFF更新（候補`3d5a45d`）。
- 同branchの巻戻し（候補`98ecc7c`、non-fast-forward）。
- 同repositoryのprotected `main`更新。
- 専用smoke branchのdelete。
- 同repositoryの`refs/tags/test/s2-4-denied`作成。
- broker helperによる別repository `jj1xgo/agent-container`へのread要求。

live advertisementと前後の全ref照合だけはGitHubへ接続する。実行前後のremote refs不変、各CLIのnonzero、guard未到達、audit、cleanupを検査する。main／既存branch／tagへの実書き込みはguardで送信不能とし、各対象を提示したfresh approval後だけ実行する。

### 承認済みcreate-only拒否gate（2026-09-07）

利用者が上記6件を直前承認し、host checkout `97e6bd4`と同じimage、準備済みguard付きdriverを1回実行した。**6件すべてPASS**。CLIの各終了値と実測秒数は次のとおり。

| case | exit | 秒数 |
| --- | --- | --- |
| 専用branchのFF更新 | 128 | 0.556 |
| 同branchのnon-fast-forward巻戻し | 128 | 0.572 |
| protected main更新 | 128 | 0.540 |
| 専用branchのdelete | 128 | 0.464 |
| non-head tag作成 | 128 | 0.480 |
| 別repositoryへのbroker helper read | 128 | 0.082 |

receive-pack送信前guardの到達回数は0。5種類のpushは`git-receive-pack`／`denied` auditを各1件、計5件残し、remote前後照合は`git-upload-pack`／`ok` 2件だった。別repository要求はclient側で拒否され、追加broker auditなし。auditは固定schema、stageなし。全remote refsは実行前後で一致し、許可外の書き込みは行われなかった。

launcher／processともexit 0、timeoutなし、既存workspaceのfile状態とHEADは不変。GitHub／egress双方のsocket・capability・run directoryと対象containerは不在。標準broker policyがguard手前で拒否した実測であり、テスト用guardによる拒否をbroker成功へ読み替えていない。

### Family実CLI gateの事前確認

既存の専用projectについて両agentのdoctorがexit 0、必須checkはすべてPASS。domain制限なしの既知network-policy WARNだけが残る。local binding、pending store、auditを検査し、既存recordはrejected 4件／created 2件、pending／unknown 0件、audit 23件でvalid。本文・送信先repository名／ID・credentialを表示していない。通常sandboxでのpending検査はlockアクセス制限により失敗したが、許可されたhost実行では成功した。既存recordやbindingは変更していない。

次の検証範囲は実Codex／Claudeを別runで起動し、各1件の固定fixtureをhost pendingへ提出、同runの2回目はduplicate denial、canonical署名・audit・cleanupを確認すること。実GitHub Issue作成は別の直前承認が必要で、この準備では実行していない。Family live installation inventory、実CLI intakeともまだnot run。

### 承認済みFamily実agent intake（2026-09-07）

利用者が既存専用projectでCodex／Claude各1回、local pending各1件と同runの重複拒否を承認した。host checkout `64e122e`と同じimage、一時driver `/tmp/s2-4-family-cli-smoke.py`から通常のagentctl runtime監視経路を使用した。実GitHub Issue作成は承認範囲に含めず、実行していない。

CodexはPASS。実agentのshell toolが指定CLIを2回実行し、初回はpending、2回目はexit 1の固定拒否。新規pendingは1件、client request IDとhost recordが一致し、canonical fixtureとhost生成署名が一致した。host preview成功、追加auditは`intake/pending/intake`と`preview/pending/validation`各1件、固定schemaでvalid。既存recordとworkspaceのGit状態は不変。launcher／processともexit 0、timeoutなし。通常container境界と単一socket file mountを維持し、Family／handoverの対象artifactとcontainerは終了後不在だった。pendingは承認待ちで保持し、本文・送信先repository名／IDは証跡へ保存していない。

Claudeはintake **not run**。launcher／processはexit 0で終了したが、tool実行0件、新規pending0件、追加audit0件だった。既存recordとGit状態は不変、通常container境界と単一socket file mountを維持し、Family／handover artifactとcontainerのcleanupはPASS。観測eventはassistant 2件、rate_limit_event 1件、result 1件、system 21件、その他1件。rate_limit_eventの存在だけでrate limitを原因と断定しない。raw応答を保存していないため未実行理由は未確定であり、process成功をintake成功へ読み替えない。

追加の実agent起動は行わず、再検証用driver `/tmp/s2-4-family-cli-smoke-v2.py`に固定候補のresult subtype、is_error、permission denial件数、Bash tool提供有無だけを記録する診断を準備し、構文検査した。応答本文やcredentialは記録しない。再実行はnot run。runtime実装・sandbox設定は変更していない。

### Family live installation inventory（2026-09-07）

初回の一時driverはmetadataの存在しない`app_id`を参照し、API接続前に失敗した。一次情報のdataclass定義に合わせて`client_id`比較へ修正。修正後の通常sandbox実行はlive inventory段階で失敗したが、同じdriverの許可済みhost実行はPASS。Family Appと開発用Appのclient IDが異なり、live inventoryのselected repositoryはexact 1件で既存bindingと一致した。App／bindingを変更せず、tokenはメモリ内で扱い最後にcacheをinvalidateした。repository名／ID、token、raw API応答は出力していない。この検査はGitHub設定画面の全権限を確認した証跡ではない。

FamilyのClaude intakeと実Issue作成等の残ゲートは未完了。S2-4全体も未完了のまま保持する。

### 診断付きClaude intake再検証（2026-09-07）

利用者がlocal pending作成と重複拒否の範囲でClaudeの追加1回を直前承認し、host checkout `d113a11`と同じimageから`/tmp/s2-4-family-cli-smoke-v2.py claude`を1回実行した。intakeは引き続き **not run**、検証driverはexit 1。実agentのlauncher／processはexit 0、timeoutなし、result subtypeは`success`、is_error false、Bash tool提供あり、permission denial 0件だったが、tool実行0件、新規pending0件、追加audit0件だった。これらはintake成功の証拠ではなく、未実行の原因も未確定。

既存pendingを含む全recordとworkspaceのGit状態は不変、audit valid。通常container境界と単一socket file mountを維持し、Family／handover artifactと対象containerは終了後不在。event件数はassistant 2、rate_limit_event 1、result 1、system 13、その他1。応答本文・credentialは保存していない。GitHub Issue作成は実行せず、追加の実agent再試行も行っていない。Claude intakeの未解決を残し、S2-4を完了扱いにしない。

### Claude通常対話経路の切り分け（2026-09-07）

利用者の継続指示に基づき、host checkout `60755b8`と同じimageで通常の`run_claude_spec`／Family supervisorを維持したPTY診断を行った。intakeを指示したrunは2回、起動画面だけを調べたrunは2回。全4回とも停止後のFamily／handover artifactと対象containerは不在、既存recordとGit状態は不変、新規pending 0件、追加audit 0件。停止には監視processへのSIGINTを使用し、launcher／processのexit 1を正常完了へ読み替えていない。

初期driverはANSI cursor更新を単純除去していたため、画面上の空白を正しく復元できず、入力待ちや承認画面の検出がfalseになった。`error`という単語だけから起動エラーを疑ったが、失敗原因としては確認できなかった。画面復元を追加し、送信先・fixture等を伏せた表示で、通常の入力待ち画面とmanual modeを確認した。端末照会への応答不足を疑った診断でもcursor queryは観測されず、その仮説は確定していない。

最後の対話runでは、Claudeは指定fixtureの直接提出に先立ち、未知のhelperの意味を確認すると述べ、`agent-family issue create --help`とファイル種別確認を組み合わせた補助commandを提案した。これは指定intake commandではなく、承認画面で待機していた。画面全体に元のcommandが含まれるだけでは実行対象との一致を証明できず、直前dialogの一致判定もfalseだった。途中で一度Enterを入力したが、補助commandの完了は観測できていない。intake／重複拒否はnot runであり、toolの実行意図や確認画面をbroker到達へ読み替えない。

この観測だけでは、以前の非対話runがtool 0件だった理由を確定できない。sandbox／App／bindingは変更せず、既に確認済みのhelper仕様を指示へ含め、指定commandが実行不能なら短い理由を返す非対話driver `/tmp/s2-4-family-cli-smoke-v3.py`を準備した。構文検査成功。最初の実行tool callは中断されたため、再開時に対象containerが不在であることを確認してから実行した。結果は次節へ記録する。

### 仕様説明付き非対話Claude診断（2026-09-07）

v3 driverはlauncher／process exit 0、result subtype `success`、is_error false、Bash提供あり、permission denial 0件で終了したが、tool実行0件、新規pending0件、追加audit0件だった。短い理由の抽出条件に適合する応答は得られず、原因は未確定。driverはexit 1、timeoutなし。既存recordとGit状態は不変、audit valid、通常境界と単一socket file mountを維持、Family／handover artifactと対象containerは不在。続いて診断部分だけを変更したv4を準備し、fixture／識別情報／command blockを伏せたagent説明部分を確認することとした。sandboxや実行許可の範囲は変更していない。

### Claude intake／duplicateの実測成功（2026-09-07）

host checkout `60755b8`と同じimageでv4 driverを実行し、**PASS**。実ClaudeのBash toolはexact commandを2回実行し、初回はpending、2回目はexit 1の固定拒否だった。新規pendingは1件、client request IDとhost recordが一致し、canonical fixtureとClaudeのhost生成署名が一致。host preview成功、追加auditは`intake/pending/intake`と`preview/pending/validation`各1件、固定schemaでvalid。既存recordとGit状態は不変。launcher／process／driverともexit 0、timeoutなし、result subtype `success`、is_error false、permission denial 0件。通常境界と単一socket file mountを維持し、Family／handover artifactと対象containerは終了後不在。

v3からv4への変更は失敗時の説明を抽出するhost側診断だけで、agentへの指示と実行許可設定は同一。今回は指定commandを実行したが、以前のtool未実行の原因を修正・特定した証拠ではない。成功済みの実CLI intake／duplicateと、未解決の実行のばらつきを区別する。実GitHub Issueは作成せず、Codex／Claude由来のpendingを各1件保持する。その他の残ゲートがあるためS2-4全体は未完了。

### 承認済みFamily実Issue create／再approve拒否（2026-09-07）

section 5に従い、その場のpreviewからexact target、request ID、canonical title／body、目的、Issue 1件作成・自動削除なしの外部影響を利用者へ提示し、対象1件のfresh explicit approvalを得た。host checkout `b37ccee`の通常`bin/agentctl family issue approve`をprivate PTYで実行。Echoを無効にし、承認済みcanonical内容・target・pending状態・期限とCLIのexact previewを照合してから確認文字列を1回だけ入力した。本文・確認文字列・credential・raw応答は証跡へ保存していない。

**PASS**: [smoke Issue #121](https://github.com/jj1xgo/agent-container/issues/121)を1件作成し、CLI exit 0、host record `created`、canonical本文消去を確認。hostのread-only `gh issue view`でnumber／URL、承認済みtitle／bodyの完全一致、OPEN、期待Family App authorを照合した。Issueは自動close／deleteせず保持する。他のpending recordは不変。

同じrequestへ通常CLIのapproveをもう1回実行すると、確認入力前にexit 1で拒否された。recordとauditは再approve前後で不変。terminal stateの検査がprovider／送信より前にある実装とも一致し、追加Issueを作成していない。作成時の追加auditは`approve/sending/send`と`approve/created/cleanup`各1件、固定schemaでvalid。いずれのCLIも中断なし。Codex由来pendingは未送信のまま保持する。残りの実host gateとrequired CI／main取り込みがあるため、S2-4全体は未完了。

### 承認済みClaude handover create gate（2026-09-07）

担当交代後の照合で、stage 2対象（S2-1 H1〜H4）の実host証拠が無い唯一の項目としてClaude handover createを特定し、利用者がprivate terminalのTUI runによる1回実施を承認した。外部影響は専用smoke projectのhandover root（host上）へfileが1件増えることだけである。host checkout `3f382db`（runtime実装は`36f02a8`と同一）、上記image、Claude Code `2.1.263`。

事前に固定7 section本文をsmoke workspaceへmode `0600`で置き、broker audit行数（103）、handover directoryの既存file（2件）、run root `r/b73fb7e09b35/`（空）、対象container（0件）を記録した。利用者が`bin/agentctl --image localhost/agent-family-test:local run agent-container-claude-smoke --agent claude`を起動し、実行中にhost側から次を記録した。run directory `d312b4050e5ce65f`はmode `0700`、`broker.sock`と`capability`はともにmode `0600`・実行user所有（本文は未読）。container `339773c654cb`は`/run/agent-handover`をread-only、`/workspace`をsmoke workspaceとしてmountし、rootfs read-only、`no-new-privileges`。

container側のsession記録（内容はcommandと固定出力だけを照合）では、tool実行は4件で、本文fileのRead、read-onlyの`git status`、指定どおりの`agent-handover create --title "Phase 6 S2-4 Claude handover create smoke" < /workspace/handover-body.md`が1回、`ls /proc | grep -c '^[0-9]\+$'`が1回だった。createのtool resultは作成path 1行のみ。

| 観測 | 結果 |
| --- | --- |
| host file | PASS。`2026-09-07_085132_6e09d10b.md`が1件だけ増え、通常file・mode `0600`・owner 1000:1000。title行、`Project`／`Created`／`Session` metadata、固定順の7 sectionを持ち、section以降は用意した本文とbyte一致。既存2 fileは残存 |
| audit | PASS。1行だけ増加（103→104）。key集合は`operation`／`path`／`project`／`run`／`stage`／`status`／`timestamp`、`create`／`ok`／`write`、projectは一致。追加行にtitleと本文sentinelを含まない |
| cleanup | PASS。通常終了後にrun directory・socket・capabilityが不在、対象containerは0件 |
| stale client | PASS。旧socket／capability pathを指定したhost側実clientの`create`はexit 1、stdout空、stderrは固定`error: handover broker request failed`、audit不変、新規fileなし。socket消失後の拒否であり、稼働中brokerへの失効済みcapability提示ではない |
| `/proc`件数 | 出力は`7`。agentは内訳を述べず、sandbox内process数との照合はPARTIAL |
| 対話確認 | 利用者報告: `/status`正常、`/hooks`はstage 1と同じ、`/mcp`なし。`/sandbox`はsession記録上`Error: Sandbox settings are overridden by a higher-priority configuration and cannot be changed locally.`で、stage 1（2026-08-26、09-06）と同じmanaged強制の表示。停止条件（managed policy未読込、sandbox無効、fallback可能、hook／MCP読込）には該当しない |
| workspace | 一時本文fileは検査後に削除し、smoke workspaceのGit状態は`main...origin/main`のまま |

これでstage 2が観測挙動を変える4 broker（handover、egress、GitHub、Family）すべてに実host証拠が揃った。gate 2〜4（read-only拒否、cross-project、malformed／secret）は自動testのみで、stage 1（6-6）と同じ範囲で受け入れる。
