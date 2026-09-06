# Changelog

このprojectは[Semantic Versioning](https://semver.org/)に従います。`0.x`の間は、CLI、state配置、security boundaryが互換性なく変更される可能性があります。

## [Unreleased]

### Added

- Phase 6 stage 1の5番目として、Family intakeのrequest／response frame codecとaccept iterationを既存の共通broker kernelへ移しました。total-frame上限、exact bytes型、JSON例外はFamily adapterで保持し、PID/uid検証、stream処理、停止・descriptor cleanup、capability、audit transactionはFamily側に残します。既存testとwire／auditは不変で、readiness gateを含む保証の統一はstage 2で扱います。

- 標準agent imageに`jq`を追加しました。agentがJSON出力(`.claude.json`、CLI応答など)を扱う際、代替commandを自作せず本物の`jq`を使えます。
- Phase 6 stage 1の最初の乗せ替えとして、共通broker kernel package `agent_container/broker/`に`frame`（length-prefixed JSON frame codecとstream helper）と`capability`（container側のexact path・capability file・socket検証と接続）を追加し、handover brokerのprotocolとcontainer側clientをその上に移しました。wire形式、audit、既存のhandover testは変更していません。kernel化前のencoderで生成したgolden byte fixtureを`tests/container/test_broker_frame_golden.py`に固定しました。
- Phase 6 stage 1の2番目の乗せ替えとして、共通broker kernelに`audit`（private append-only audit log）、`readiness`（`ReadinessGate`と`AlwaysReady`）、`runtime`（run directory・capability・private socketの生成回収と、accept loop・stop・error captureの`SocketBrokerRuntime`）を追加し、handover brokerのsessionとruntimeをその上に移しました。audit行、error message、停止順序、既存のhandover testは変更していません。kernel化前のwriterで生成したaudit行のgolden fixtureを`tests/container/test_broker_audit_golden.py`に固定しました。
- Phase 6 stage 1の3番目の乗せ替えとして、egress brokerのprotocol、session、runtimeを共通broker kernelの上に移しました。kernelには`FrameSchema.frame_label`（framing／JSON errorの接頭辞をschema errorと分ける）、`create_private_file`の`mode`、`open_connection`、`SocketBrokerRuntime`の`concurrency="thread"`（接続毎のworker threadと回収）・`wait_failed`・`raw_client`・`deactivate_after_join`を既定値を変えずに追加しました。wire byte、audit行、error message、停止順序、既存のegress testは変更していません。kernel化前のencoderとwriterで生成したgolden fixtureを`tests/container/test_broker_egress_golden.py`に固定しました。container側の`egress_adapter.py`は互換性のため据え置き、capability処理の完全統一はstage 2に残します。
- Phase 6 stage 1の4番目の乗せ替えとして、GitHub brokerのframe／chunk、accept iteration、run directory確保、audit writeを共通broker kernelへ移しました。既存のJSON例外、lifecycle、cleanup、capability検証とaudit openerは互換性のためGitHub側に維持し、完全統一をstage 2に残します。wire byte、audit行、error identity、停止順序と既存testは変更していません。

### Fixed

- managed profile version `1`（2026-08-22の初期profile。approval rulesはversion `2`で追加）でseedしたprojectには`CODEX_HOME/rules/`が存在せず、`bin/agentctl project update-profile PROJECT`が`rules/default.rules`の`FileNotFoundError`で失敗して、後続のprofile修正（専用handover rule、sandbox network設定）を受け取れませんでした（`agent-container-claude-smoke`で観測）。update-profileは`rules/`が無い場合だけprofileの`rules/`を`project add`と同じ方法で配布してから既存の更新を続け、`rules/`がsymlinkなら拒否します。既存の`rules/`やcustom ruleの扱い、config／skill／version fileの必須条件は変わりません。

- `[sandbox_workspace_write] network_access = true`は新規projectの`CODEX_HOME`にしか配布されず、以前に作ったprojectのCodex sandbox内commandは修正後のimageでも`agent-family issue create`がEPERMで失敗していました（2026-09-06の実認証Family intake再実行で観測）。`bin/agentctl project update-profile PROJECT`が既存`config.toml`のmodelやstatus lineなど他のkeyを保持したまま同設定だけを保証するようにし（tableが無ければ追記、`network_access = false`は同じ位置で置換、symlinkは拒否）、managed profile versionを`4`にしました。

- Codex sandbox内のtool commandが`agent-family issue create`でbroker socketへ接続できず、`error: family intake request failed`（exit 1）になっていました。Codex 0.153.4のLinux sandboxは既定でcommandのnetworkを切り（`CODEX_SANDBOX_NETWORK_DISABLED=1`、追加seccomp filter）、`socket(AF_INET)`と`connect(AF_UNIX)`をEPERMで拒否します。`profiles/codex/config.toml`に`[sandbox_workspace_write] network_access = true`を追加し、sandbox内commandにPodman containerと同じnetwork到達性を与えます。`--github-broker`のgit操作とegress proxy経由のcommandも同じ経路で失敗していたはずで、この設定で解消します。`sandbox_mode`、`default_permissions`、`features.network_proxy`は設定しません。

- `run`のCodex／Claude runtime containerで、Codexのbubblewrap sandboxが`bwrap: Can't mount proc on /proc: Operation not permitted`で失敗し、sandbox内のcommand実行が全て失敗していました。Podmanが既定で付ける`/proc`のmasked（`/proc/kcore`等6件）とread-only（`/proc/sys`等6件）のsubmountが、bwrapのuser namespace作成時にlocked child mountとなり、kernelの`mount_too_revealing`／`mnt_already_visible`（`fs/namespace.c`）が「空でないdirectoryやfileを覆うlocked child mountがある」として新しいproc mountを拒否していました。masked側だけ、read-only側だけの解除では解消しません。agent runtime specに`--security-opt=unmask=/proc/*`を追加し、doctor probe・setup・build用containerには付けません。`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、tmpfs、mount構成、`/sys`側のmaskは変更していません。

- egressで未許可CONNECTを拒否すると、adapterだけがsequenceを進めて後続の許可済み通信も拒否されていました。並行requestの到着順逆転も同じ不整合を起こしました。brokerは認証済みrequestの直近4,096番号を固定bitmapで追跡し、到着順が逆でも未使用番号を受け付け、policy拒否の番号も消費します。再送・範囲より古い番号は拒否し、未認証requestは追跡状態を変えません。これは`v0.5.0`でも再現した既存不具合の独立修正で、wire・許可domain・audit schemaは変更していません。

- broker workerの登録後・開始前にstopが重なると、未開始threadのjoinが`RuntimeError`を出し、egressの失効処理にも到達していませんでした。未開始workerを管理対象に残し、停止未完了を所定の例外で報告して、開始後の再試行で回収できるようにしました（[#97](https://github.com/jj1xgo/agent-container/issues/97)）。
- Claude launcherが設定する`IS_DEMO=1`はtoken onboardingだけでなくworkspace trust dialogも省略するため、project configの`.claude.json`に`hasTrustDialogAccepted`が残らず、managed status lineを含むtrust前提の機能が黙って動いていませんでした。launcherは起動直前に現在のworkspaceの該当keyだけをseedし、fileが無ければmode `0600`で作り、通常fileでない・実行user所有でない・JSON objectとして読めない場合は本文を出さずに起動を停止します。permission bypass optionやproject側hooks／MCPの扱いは変わりませんが、trust承認と同じくworkspace内`.claude/settings*.json`の`permissions.allow`と`additionalDirectories`は有効になります。

### Security boundaries

- Codex sandbox（workspace-write）内のtool commandは、Codex本体processと同じnetwork到達性を持つようになります。network境界はPodman側（`--network=none`＋egress adapterのexact-domain allowlist、または制限なしprojectの通常network）で与えるため、containerの外へ新しい経路は増えません。sandbox内command同士やcontainer内loopbackへのTCP／Unix socket接続は可能になり、Unix socketのpath単位allowlistは、Codexの`network_proxy`機能が直接`connect`する現在のbroker clientと両立しないため採用していません。read-only sandboxのnetwork無効は変わりません。

- agent runtimeの`/proc` unmaskにより、container内uid 1000（keep-id、全capability削除）は`/proc/keys`と`/proc/interrupts`を読め、`/proc/acpi`と`/proc/scsi`を一覧できるようになります。`/proc/kcore`と`/proc/timer_list`は引き続き読めず、`/proc/sys/*`と`/proc/sysrq-trigger`への書き込みはroot所有fileに対するDACとcap-dropで拒否されます。`/proc/sys`の書き込み禁止はmount flagではなく非root uidとcapability削除に依存する形へ変わります。`/sys/firmware`、`/sys/fs/selinux`、`/sys/fs/cgroup`等のmaskと、probe／setup containerの既定maskは維持します。

- Claude managed policyに`allowManagedPermissionRulesOnly`をpinしない判断を記録しました。Claude Code 2.1.260ではこの設定がpermission promptの「don't ask again」も無効にするため、利用者自身のrepositoryだけを動かす現状では、workspaceの`.claude/settings*.json`のallow ruleをtrust承認と同じく受け入れます。managed `deny`、sandbox、bypass禁止、brokerのhost承認は変わりません。中身を確認していない第三者repositoryを動かす際に見直します。

### Validation

- 2026-09-06、Phase 6-6のClaude handover create gateを、使い捨てのClaude専用smoke project `agent-container-claude-smoke`で、利用者のprivate terminalから`bin/agentctl --image localhost/agent-family-test:local run agent-container-claude-smoke --agent claude`（専用image `edd9916b52f3`）を起動して1回実施しました。固定の7 section本文はhost側でworkspace内にmode 600の一時fileとして用意し、sandbox内のBash toolから`agent-handover create --title "Phase 6-6 Claude handover create smoke" < /workspace/handover-body.md`を1回実行しました。stdoutは作成pathのみで、host側`/home/tsu/obsidian-vault/handovers/agent-container-claude-smoke/`に通常file（mode 600、owner 1000:1000、33行）が1件増え、`Project`／`Created`／`Session`のcanonical metadataと固定順の7 sectionを持ち、本文は用意した固定fileとbyte一致しました。handover broker auditは`create`／`ok`／stage `write`の固定field 1行だけ増加（38→39行）で、title・本文・capabilityを含みません。通常終了後、containerは開始前からの1件のみ、当該runのsocketとcapabilityは消滅（project単位の空directoryのみ残存）、一時fileは削除済みです。これで6-6の残gateはrollback（`not run — 継続検証用にApp bindingを保持`）だけです。最初に利用者から伝えられたpathは未来時刻のfilenameでhost側に実体がなく、broker audit・session記録にも痕跡がなかったため採用せず、再実施した結果だけを記録しています。

- 2026-09-06、Phase 6-6のCodex handover create gateを、使い捨てsmoke project `agent-container-smoke`（egress allowlist有効、managed profile version 4）で1回実施しました。通常の`agentctl run`経路（preflight、egress runtime、supervisor）を保ち、`runtime_spec_builder`でagent commandだけを`codex --approve-for-me -c … exec --ephemeral --json`へ置換（`--interactive`／`--tty`除去）した非対話runで、専用image `edd9916b52f3`のCodexは指示どおり`agent-handover create --title "Phase 6-6 Codex handover create smoke"`だけを1回実行し（`command_execution` 1件、exit 0）、stdoutは作成pathのみ、runtime exit 0でした。host側`/home/tsu/handovers/agent-container-smoke/`には通常file（mode 644、owner 1000:1000、19行）が1件増え、`Project`／`Created`／`Session`（`CODEX_SESSION_ID`を記録）のcanonical metadataと固定順の7 sectionを持ち、既存の2026-08-29のfileは不変でした。egress auditは`connect` ok 17件と起動直後の`connect`／`policy` denied 1件（認証拒否0件、固定fieldのみ）で、終了後のcontainerは開始前からの1件のみ、新規socketなし、egress run directoryは既知の2件のままです。Codex経路はbrokerを使わないproject別direct writerのため、本文を空のtemplateとして作成しsectionは埋めていません。raw agent出力はscratchpadだけに保持しています。

- 2026-09-06、Phase 6-6のClaude実host再確認を、利用者のprivate terminalから`bin/agentctl --image localhost/agent-family-test:local run findsummits --agent claude`（専用image `edd9916b52f3`、Claude Code 2.1.261、production／profileはmain `e44cd1e`と同一）で1回実施しました。`/sandbox`は「higher-priority configurationによりlocal変更不可」（2026-08-26と同じmanaged強制の表示）、`/mcp`は0件でした。`/hooks`は「1 hook configured」と「Hooks Restricted by Policy」を表示し、手順書の停止条件に該当したため一旦停止して切り分けました。原因は`findsummits`のClaude configへ2026-09-02にuser scopeで導入済みの`superpowers@claude-plugins-official` 6.3.0が持つ`SessionStart` hook 1件で、`/proc` unmask（#108）とも、本projectのmanaged policy／project settings（いずれもhook定義なし）とも無関係です。Claude Code hooks文書（`allowManagedHooksOnly`の項）は「user、project、local、plugin hookはblockされ、managed `enabledPlugins`で強制有効化したpluginのhookだけが例外」と明記しており、本managed policyに`enabledPlugins`はありません。container側の全session記録7件（09-02〜09-06）で、hook発火時に記録される`SessionStart`／`hook_event_name`／`additionalContext`が0件（superpowersが有効なhost sessionでは同記録が多数、旧sessionの`superpowers`文字列は`skill_listing`のみ）であることから「設定上は存在するがpolicyでblockされ未発火」と判断し、利用者承認のうえ証拠付きPASSとして再開しました。再開後、container内Claudeは最初に`/workspace`と`pip`だけを探索してprobe module不在と判断しましたが、moduleは`/opt/agent-container/src`（image envの`PYTHONPATH`）に実在し、実行指示でsandbox内のBash toolから通常の`python3 -m agent_container.claude_security_probe`（`PYTHONPATH`付与なし）がexit 0で`oauth_token_visible=false`、`token_file_readable=false`、`parent_token_via_proc_readable=false`の3行だけを出力し、`/proc` unmask後の実hostでも維持されていることを確認しました。続けてsandbox内から`agent-family issue create`を2回実行し、1回目は`pending`／request ID／expiryだけでexit 0、同runの2回目は`family intake request failed`（exit 1、one-time capability消費済み）でした。host auditは`intake`／`pending`（stage `intake`）とhost previewの`preview`／`pending`（stage `validation`）の固定6 field 2行だけ増加（18→20行）、pendingに`8e360eaa47262c4d47dc3bbddd6fccbe`（state pending）が1件追加、previewは末尾に`— Claude (findsummits)`署名が1回だけでCodex署名なし、本文は転記していません。通常終了後、`findsummits`と専用imageのcontainerは全stateで0件、family intake directoryは既知の空directory 2件のまま、inspection markerなし、残存socketは2026-09-05起動の`agent-container` project container（`104c23e4383f`）へbind mountされたhandover brokerのものだけでした。Claude sandbox再確認、security probe、Family Claude intake、対話TUI経路の各gateはPASSです。request `8e360eaa…`のapprove／rejectは利用者の判断に委ね、GitHub Issueは作成していません。rollback（section 7）は`not run — 継続検証用にApp bindingを保持`、Codex／Claude handover createは`not run — 次のblockで扱う`です。

- 2026-09-06、section 5のSTOPで6項目（exact target repository、request ID、canonical title、canonical body、purpose、external effect）を提示し、利用者がprivateな対話terminalで`bin/agentctl family issue approve findsummits 6b119494dca9a4f6fd11400c2347c331`を実行して確認文を入力し、[Issue #111](https://github.com/jj1xgo/agent-container/issues/111)を1件作成しました。それに先立つ`!`経由（TTYなし）の実行は`approval refused: interactive TTY required`、対話terminalでの最初の入力は不一致で`approval cancelled`となり、いずれもaudit `approve`／`validation`／`denied`の記録だけで外部操作はありませんでした。作成後はhost側でstate `created`、terminal recordはtitle／bodyが消去され`issue_number`／`issue_url`だけを保持、auditは`approve`の`send`／`sending`と`cleanup`／`created`の固定field 2行だけ増加（15→17行）、GitHub側はOPEN・title一致・author `app/jj1xgo-family-issue-broker`・本文末尾の署名1回を確認しました。再approveはTTYなしの試行で送信前に拒否され、対話terminalでの再approveは`not run`です。作成済みIssueは変更していません。

- 2026-09-06、利用者指示により基準`9e61522`の`bin/agentctl project update-profile`を`findsummits`以外の既存projectへ適用しました。`agent-container`、`agent-container-smoke`、`ghb-smoke`、`sotlas-frontend`（いずれもmanaged profile version 3）は既存`config.toml`を保持したまま末尾に`[sandbox_workspace_write]`／`network_access = true`が追記され、version 4、mode 0600でした。`agent-container-claude-smoke`（version 1、Claude専用）は`rules/default.rules`が無く`update-profile`が失敗して未更新、`agent-container-broker-smoke`はcodex-homeが無く対象外です。Claude専用projectのCodex profileは別途扱います。

- 2026-09-06、PR #110取り込み後の基準`9da320e`（production／profile／CIはmain `e44cd1e`と一致）から再buildした専用image `edd9916b52f3`で、手順書のlocal実Podman 16件（152.455秒、skip 0）が成功しました。続けて同checkoutの`bin/agentctl project update-profile findsummits`を適用し、`CODEX_HOME/config.toml`は既存23行を保持したまま末尾に`[sandbox_workspace_write]`／`network_access = true`が追記され、managed profile version 3→4、mode 0600不変、rules 10行不変でした。その後、承認済みの実認証Family Codex intakeを通常の`agentctl run findsummits`経路（preflight、Family runtime、PID登録、supervisor）でagent commandだけを`codex --approve-for-me exec --ephemeral --json`へ置換して1回実行しました。runtime exit 0、`command_execution` 2件で、1回目の`agent-family issue create`は`pending`／request ID／expiryだけを出力してexit 0、同runの2回目は`family intake request failed`でexit 1（one-time capability消費済み）でした。host auditは`intake`／`pending`の固定field 1行だけ増え（10→11行）、pendingに`6b119494dca9a4f6fd11400c2347c331`（state pending、期限1788746010）が1件追加、previewはfixtureのcanonical titleと一致し末尾に`— Codex (findsummits)`署名が1回だけあり、credential／repository ID／approval commandは本文に含まれません。container、run directory、workspace statusは不変です。raw agent出力はscratchpadだけに保持し本文は転記していません。Family Codex intake gateはPASSで、GitHub Issue作成は行っておらず、request `6b119494…`のapprove／rejectは利用者の判断に委ねます。

- 2026-09-06、PR #109取り込み後の基準`2971992`から再buildした専用image `3230c6be7b58`（新profileを含むことをimage内で確認）で、手順書のlocal実Podman 16件（150.174秒、skip 0）が成功し、続けて承認済みの実認証Family Codex intakeを同じ経路（今回は`--sandbox`flagを外し対話runtimeと同じ`codex --approve-for-me exec`）で1回実行しました。runtime exit 0、`command_execution` 2件で、いずれも`error: family intake request failed`（exit 1）、host audit 10行のまま、pending新規0件、workspace不変、container回収済みで、intake gateはFAILのままでした。原因は`findsummits`の`CODEX_HOME/config.toml`が2026-09-02の`project add`時に配布されたままで（436 bytes、`sandbox_workspace_write`なし）、`seed_codex_home`は初回だけ配布し`update-profile`もrulesとskillしか更新していなかったことです。独立修正PR #110をmain `e44cd1e`へmergeし、smoke branchへ`6ef8793`で取り込みました。`update-profile`のlive stateへの適用と実認証intake再実行は次項以降に記録します。

- 2026-09-06、基準main `f2a2afd`からのmanaged profile更新修正を確認しました。`update-profile`が旧`config.toml`へtableを追記する場合、`network_access = false`を同位置で置換する場合、symlinkを拒否する場合と冪等性をunit testで固定し、container 1,123件、docs test 67件、lint、whitespace検査が成功しました。実host `findsummits`の現行`config.toml`（2026-09-02配布、`marketplaces`／`plugins`／`projects`／`tui`を含む）のcopyに対する適用では、既存本文を先頭からbyte一致で保持して`[sandbox_workspace_write]`を末尾に追記し、再適用で不変でした。live stateへの適用（`bin/agentctl project update-profile findsummits`）と実認証Family Codex intake再実行は`not run — merge後に実施`、local実Podmanは`not run — runtime／imageの変更なし（required CIで実行）`です。

- 2026-09-06、利用者承認により基準`11614ce`と再build済み専用image `8407957081a1`（Podman 5.8.6、crun 1.28、Codex 0.153.4）で、通常の`agentctl run findsummits`経路（preflight、Family runtime、PID登録、supervisor）を保ちagent commandだけを非対話`codex --approve-for-me exec --sandbox workspace-write --ephemeral --json`へ置換した実認証Family Codex intakeを1回実行しました。runtime exit 0、event 5種、agent message 2件、`command_execution` 2件（修正#108前は0件）で、Codexは指示どおり固定fixtureの`agent-family issue create`を2回実行しましたが、いずれも`error: family intake request failed`（exit 1）でした。host auditは10行のまま、pending新規0件、workspace status不変、container回収済みで、intake gateはFAILのままです。raw agent出力はscratchpadだけに保存し本文は記録していません。続くoffline再現（一時state＋実Family runtime＋実spec argv）で、Codex sandbox内からは`stat`成功・`connect(AF_UNIX)` EPERM、同じcontainerのsandbox外からは`pending`受付・2回目拒否を観測し、原因をCodex sandboxのnetwork無効化に絞りました。修正はPR #109として独立して行い、main `f2a2afd`をsmoke branchへ`ab2aadd`で取り込みました。offlineの模擬Responses API経由では、新profileでsandbox内`agent-family issue create`が`pending`を返しaudit +1、旧profileでは失敗することを確認しています。修正版imageでの実認証intake再実行は次項の準備後に行います。

- 2026-09-06、基準main `425e944`からのCodex sandbox network修正を、rootless Podman 5.8.6、crun 1.28、専用image `8407957081a1`（Codex 0.153.4）で確認しました。offlineのloopback模擬Responses API（`--network=none`、credentialなし）が`exec_command` 1件を返し、sandbox内の固定probeが修正前は`CODEX_SANDBOX_NETWORK_DISABLED=1`・`connect(AF_UNIX)` EPERM・`socket(AF_INET)` EPERM、修正後は両方接続成功・追加seccomp filterなしとなることを、実runtimeと同じ`codex --approve-for-me exec`（`--sandbox`flagなし）と`--sandbox workspace-write`の両方で観測しました。`--sandbox read-only`ではnetworkは無効のままです。permissions profileのUnix socket allowlist単体は効果がなく、`network_proxy`有効時は`socket(AF_UNIX)`自体がEPERMになるため採用していません。新規`tests/integration/test_codex_sandbox_network_podman.py`は旧profileでRED（`network_disabled_env='1'`、`unix_connect=failed:PermissionError:1`）、新profileでGREENでした。container 1,120件、Codex 49件、broker socket 8件＋Family socket／forced-unknown 14件、lint、whitespace検査、同imageを指定したlocal実Podman 16件（147.352秒、skip 0）が成功し、検証containerは回収済みです。CIのPodman gateはmoduleを追加して固定件数を16へ更新しました。実認証のFamily Codex intake再実行は`not run — merge後にimageを再buildして実施`です。

- 2026-09-06に独立修正PR #108をmain `425e944`へmergeし、smoke branchへ`8a67095`で取り込みました。mainとsmoke branchのproduction／profile／Containerfile／CI／container scriptの一致を確認し、Family手順とkernel設計の固定期待値をcontainer 1120・real Podman 15へ更新しました（操作command、検証項目、skip禁止、外部操作の承認条件は不変）。このtreeでcontainer suite 1,120件（docs test含む）とwhitespace検査が成功しました。修正版runtimeでの実認証Family Codex intake再実行と、実hostでのClaude sandbox再確認は`not run — 利用者の停止指示を維持`です。

- 2026-09-06、基準main `7a9e927`からの`/proc` unmask修正を、host kernel 7.2.3、rootless Podman 5.8.6、crun 1.28、専用image `c0607c9fa48b`（bubblewrap 0.12.0、Codex 0.153.4）で確認しました。修正前は`run_codex_spec`の実argv（`--interactive`／`--tty`除去、agent commandを`codex --sandbox workspace-write sandbox -- /usr/bin/printf`へ置換）で`bwrap: Can't mount proc on /proc: Operation not permitted`のexit 1、修正後はexit 0と出力一致を新規`tests/integration/test_agent_sandbox_podman.py`で観測しました。切り分けでは、基本OCI制限下のbwrapで`--ro-bind / /`と`--unshare-user --unshare-pid`はexit 0、`--proc /proc`追加でexit 1、masked 6件のみまたはread-only 6件のみのunmaskでは失敗、両方で成功でした。修正後のunit test（runtime specへの付与とprobe specへの非付与）を含むcontainer 1,120件、Codex 48件、broker socket 8件、Family socket／forced-unknown 14件、lint、whitespace検査、同imageを指定したlocal実Podman 15件（103.035秒、skip 0）が成功し、検証containerは回収済みです。CIのPodman gateは`tests.integration.test_agent_sandbox_podman`を追加して固定件数を15件へ更新しました。Claude runtimeへの付与はunit testとargv検査だけで、実hostでのClaude sandboxと`parent_token_via_proc_readable=false`の再確認は`not run — 6-6のhandover blockで扱う`、実認証のFamily Codex intake再実行は`not run — 利用者の停止指示を維持`です。

- 2026-09-05、基準`da798a4`でsandbox失敗の位置を外部接続なしで切り分けました。同じ基本OCI制限・専用image・credential／project mountなしでbubblewrap 0.12.0の`/usr/bin/true`を比較し、rootのread-only bindはexit 0、user＋PID namespace追加もexit 0、その条件へ`--proc /proc`を加えた場合だけexit 1（`Can't mount proc on /proc: Operation not permitted`）でした。rootless Podmanのhost metadataはAppArmor／SELinuxとも無効、seccomp有効でprofileは`/usr/share/containers/seccomp.json`でした。同profileのmount／unshare／clone／fsopen／fsmount等は無条件ALLOWであり、一般的なnamespace作成不能やその標準profileのsyscall禁止だけではこの差分を説明できません。コンテナ内`/proc`には既定のmasked／read-only submountが存在することを確認しましたが、それが拒否原因であることは未証明です。mask、seccomp、capability、sandboxの変更は行っていません。kernel内部の拒否条件と安全な修正は未確定で、再現用の最小fixtureを`/tmp`に準備しました。実認証の追加実行、production／live state変更はありません。

- 2026-09-05、基準`ce9da65`で外部接続なしの実CLI再現を追加しました。専用image `c0607c9fa48b`、通常の基本OCI制限、`--network=none`、credential／project state mountなし、privateな使い捨てCODEX_HOME／writable workspace、container内loopbackの模擬Responses APIを使用しました。模擬応答から固定`exec_command`を1件返すと、CLIはfunction-call結果として`bwrap: Can't mount proc on /proc: Operation not permitted`（command exit 1）を模擬APIへ返しました。`--approve-for-me`あり／なしの両条件で同じ失敗となり、CLI全体は模擬の最終返答でexit 0、stdoutの`command_execution` eventは両条件で0件でした。したがって過去のevent 0件から「toolを呼ばなかった」とした解釈を撤回します。確認できるのはevent未観測とpending／audit追加0件であり、tool呼び出し前か内部実行時かは元の記録だけでは判定できません。初回のfixtureはworkspaceがread-onlyで`.git`保護用file作成に失敗したため、writable workspaceへ修正して上記結果を得ました。通常の承認引数の欠落はharnessの差分ですが、少なくともこのoffline再現では復元だけで解消しません。実認証runとの因果関係、具体的な安全な修正は未確定です。実認証再実行・sandbox緩和・live state変更は行っていません。

- 2026-09-05、基準`5752826`で利用者指示により実認証の追加実行を止め、外部接続なしで診断harnessを見直しました。通常の基本OCI制限下の直接`printf`はexit 0、通常の`--approve-for-me`を保持したexec引数のCLI helpもexit 0でした。これは前項のsandbox内`/proc` mount失敗と区別し、実認証runの原因は未確定のままとします。一時probeは通常specの引数・environment・pass_fdsを保持し、stdout／stderrを別々に捕捉する構成へ修正しました。成功には固定commandの完了・exit 0・出力一致・turn完了を要求し、自己申告、stderr由来event、失敗turn、想定外tool等を成功扱いしません。補助関数11件とfake runtimeで実際のpipe捕捉を通す2件の計13件がPASSしました（network／認証／live pending操作なし）。これらは一時harness用testであり、repositoryのcontainer suite件数1119には加算しません。import時にruntimeが起動しないことも確認しました。production、live policy、認証設定は変更せず、見直し後の実認証probeは`not run — 利用者の停止意向を維持`です。

- 2026-09-05、基準`14fdd14`で承認済みの固定`printf`診断をCodex 1回で実行しました。runtime exit 0、agent message 2件、tool item 0件で、Codexの返答は固定分類`TOOL_REJECTED`でした。実際のtool拒否eventはなく、自己申告だけで拒否原因を断定しません。pending／audit追加0件、container／Family runtime回収、workspace status不変を確認しました。続くローカル照合で、これまでの非対話Family診断builderが通常`run_codex_spec`の`--approve-for-me`を除去していたことを発見しました。過去の「同一設定」は通常runtimeの承認経路までの一致を意味せず、検証harness側の差分として訂正します。インストール済みCLIのhelpでは同optionはworkspace-writeでのautomatic reviewと説明され、bypassとは別です。credential・project mountなし、`--network=none`、通常runtimeと同じ基本OCI制限の一時containerで、正しいCLI書式`codex --sandbox workspace-write sandbox -- /usr/bin/printf PHASE66_SHELL_OK`は`bwrap: Can't mount proc on /proc: Operation not permitted`でexit 1でした。これは独立したlocal再現であり、実認証runの未実行原因との因果関係は未確定です。通常起動引数を保持する次回probeを準備しましたが`not run — 追加の実認証実行前`です。sandbox／OCI制限／policyの緩和は行っていません。

- 2026-09-05、基準`f1afd82`で承認済みFamily Codex診断を同一設定で1回実行しました。runtime exit 0、JSON event 5件、agent message 2件、turn completed 1件で、command／MCP等のtool itemは0件でした。pending／audit追加も0件でintake gateはFAILのままです。本文の固定語句分類では否定表現候補のみを検出しましたが、原因や実際のtool提供状況は確定できません。timeoutとcapture上限超過はなく、container／Family runtime回収、既存audit／workspace status不変を確認しました。ローカルconfigにtool無効化の明示設定は見つからず、credentialをmountしない`--network=none`の別containerによる`codex features list`では`shell_tool`／`unified_exec`ともtrueでした。これは実認証runのtool提供を証明しません。[公式JSON event仕様](https://learn.chatgpt.com/docs/non-interactive-mode)と[feature仕様](https://learn.chatgpt.com/docs/config-file/config-reference)も照合しました。次はFamily提出を伴わない固定`printf`のtool実行を切り分ける診断で、準備済み・`not run — 個別実行前`です。認証・model・sandbox・policyは変更していません。

- 2026-09-05、基準`3f5a859`と専用image `c0607c9fa48b`で、個別承認された`findsummits`の通常Codexを1回起動し、固定fixtureの受付と同runの2回目拒否を指示しました。通常のFamily supervisor／PID登録を保ち、Codex execは非対話・ephemeral・workspace-writeで実行しました。runtime exit 0でしたが、新規pending 0件／audit追加0件で受付は未達、intake gateはFAILです。検証scriptが識別したintake tool callも0件でした。raw runtime出力はメモリ内で検査後に破棄しており、未提出の原因は特定できません。family runtime cleanup、container回収、既存auditとworkspace Git statusの不変を確認しました。GitHub Issue作成、policy変更、自動再実行はしていません。次回診断用に本文を出さないevent件数・message分類の記録を準備し、実行は`not run — 再実行前`です。

- 2026-09-05、基準`2fed914`でFamily実intakeの準備を行いました。`findsummits`のFamily local doctorはstate／binding／pending／audit／App metadata permissionsがPASS、live inventoryはこの確認では`not run`です。専用image `c0607c9fa48b`を指定した通常Codex doctorも必須項目PASSで、外向きdomain制限なしのWARNを確認しました。追加調査した任意の`--github-broker` doctorはproject policy未配置でFAILでした。このoptionを有効化したり、policy／binding／認証設定を変更したりしていません。固定fixtureを既存schemaで検証し、1回目pending／同runの2回目拒否を指示するprivate promptを準備しました。実Codex intake、pending新規作成、実Issue作成は`not run — 対象runtimeの実行前`です。

- 2026-09-05、基準`3c6502a`と修正版image `c0607c9fa48b282befa1054363630b33f388804082c939a4666d3349ca195029`で、個別承認により`ab.chatgpt.com`を追加し、`chatgpt.com`／`pypi.org`／`sdmntprsouthcentralus.oaiusercontent.com`と合わせた4 domainでCodex 0.153.4を1回実行しました。通常mount／managed adapter／`--network=none`、非対話・ephemeral・read-onlyの最小応答をメモリ内で照合し、自然終了exit 0と応答一致を確認しました。このCodex runtime gateはPASSです。`github.com`のpolicy拒否を維持し、拒否requestより大きいsequenceの受理、authentication denial 0件、作成tunnel 13件、audit ok 13／policy denial 1件と固定field検査成功を観測しました。container／broker thread／run directory回収と追加後policyのbyte不変を確認し、通信本文・raw agent出力は保存していません。許可先は4件を保持し、rollback／Claude egress／Family実intake・実Issue／handoverなどの未実施gateへ成功を読み替えません。Phase 6-6全体は未完了です。

- 2026-09-05、基準`9f5b4de`と修正版image `c0607c9fa48b`で、個別承認により`sdmntprsouthcentralus.oaiusercontent.com`を追加し、既存`chatgpt.com`／`pypi.org`と合わせた3 domainでCodexの最小応答要求を1回実行しました。新たな未許可候補`ab.chatgpt.com`を検出して停止し、応答成功は未確認で実runtime gateはFAILです。候補の用途は未確定です。既知の`github.com`拒否を維持し、拒否requestより大きいsequenceの受理、authentication denial 0件、作成tunnel 13件、audit ok 13／policy denial 2件と固定field検査成功を観測しました。停止後のprocess exit 0を推論成功には数えません。container／broker thread／run directory回収、追加後policyのbyte不変を確認しました。許可先は3件のまま保持し、通信本文・raw agent出力は保存せず、自動再実行・追加許可はしていません。

- 2026-09-05、基準`f592974`と修正版image `c0607c9fa48b`で、個別承認されたCodexの非対話・ephemeral・read-only最小応答要求を1回実行しました。通常mount／managed adapter／`--network=none`、許可先`chatgpt.com`／`pypi.org`を維持し、既知の`github.com`拒否後も継続しました。拒否されたrequestより大きいsequenceの受理を観測し、authentication denialは0件でした。一方、新たな未許可候補`sdmntprsouthcentralus.oaiusercontent.com`を検出して停止したため、最小応答成功は未確認で実runtime gateはFAILです。候補の用途は未確定です。作成tunnel 7件、audit 9件（ok 6／policy denial 2／relay error 1）、固定field検査成功を記録しました。停止後のprocess exit 0は推論成功に数えません。container／broker thread／run directory回収とpolicyのbyte不変を確認し、通信本文・raw agent出力は保存せず、domain追加・自動再実行はしていません。

- 2026-09-05に独立修正PR #107をmain `7a9e927`へmergeし、smoke branchへ`fe24314`で取り込みました。mainと修正commit `1eb7ad9`のtree一致、smoke branchのproduction／profile／Containerfile一致を確認しました。修正版の専用image `c0607c9fa48b282befa1054363630b33f388804082c939a4666d3349ca195029`（Node v26.8.1／Codex 0.153.4／Claude 2.1.261）のbuildと、同imageを指定したlocal実Podman 14件が成功しました（108.361秒、skip 0、ResourceWarning 0）。開始前からのcontainer 1件のみが残り、検証containerは回収済みです。Family手順のcontainer期待値を回帰test追加後の1119へ更新し、docs test 67件とwhitespace検査も成功しました。修正版の認証済みegress runtimeは`not run — 再実行の個別承認前`で、6-6は未完了です。以下の修正前の観測記録は当時の結果として保持します。

- 2026-09-05の承認済みegress診断1回で、未許可`github.com`要求時のproxy socket保持者が`git-remote-http`、祖先のGit操作が`ls-remote`であることを観測しました。後続のauthentication denial 8件はすべてsequence不一致でした。外部接続なしの決定的再現により、policy拒否後の許可済みrequest、および到着順が逆転したrequestが連番不一致で拒否され続ける問題を現在のsrcと公開`v0.5.0`の両方で確認しました。kernel共通化より前からの既存不具合で、修正・Issue登録はまだ行っていません。[診断記録とreproducer](docs/superpowers/plans/2026-09-05-egress-sequence-investigation.md)に根拠を保存しました。実runtime gateはFAIL、policyの2 domainは不変、診断container／brokerは回収済みです。

- Phase 6-6 egressの実runtime gateは2026-09-05、個別承認により専用projectへ`chatgpt.com`だけを追加し、`pypi.org`と合わせた2 domainでCodexを1回起動しました（基準`4eaf808`、専用image `129ea06302ee`）。実行中に未許可`github.com`の要求を観測したため停止し、最小応答成功は未確認でgateはFAILです。停止後のprocess exit 0を推論成功には数えません。作成tunnel 1件、auditはok 1／policy denial 1／authentication denial 7件で、認証拒否の原因は未確定です。container／broker thread／run directoryを回収し、追加後のpolicyはbyte不変です。ローカル設定にGitHub上の既知superpowers marketplace URLがあることは確認しましたが、今回の接続元処理との対応は未確定です。audit検証scriptのbyte-count field名誤りは既存の`bytes_from_client`／`bytes_from_upstream`に合わせて保存済みauditをローカル再検証し、固定fieldを確認しました。通信本文・raw agent出力は保存せず、自動再実行・`github.com`追加・policy解除はしていません。

- Phase 6-6 egressの承認済みdiscoveryを2026-09-05、基準`bdb238c`（productionは`e2ce4a9`と同一）と専用image `129ea06302ee`で1回実施しました。`agent-container-smoke`の追加許可は`pypi.org`だけに保ち、通常のCodex runtime mount／`--network=none`／managed adapterで実Codex 0.153.4の非対話・ephemeral・read-only最小応答要求を起動しました。認証・schema検証後のpolicy拒否で観測したexact domain候補は`chatgpt.com`です。最初の拒否後に停止し、runtime exit 1、作成tunnel 0件、audit policy denial 1件、container／broker thread／run directory回収を確認しました。policyはbyte不変です。通信本文・credential・raw agent出力は保持せず、domainはdiscovery候補としてだけ記録し、production auditへのdomain追加はしていません。推論成功は`not run — discoveryで停止`で、候補domainの許可追加と次の実runtime操作は個別承認待ちです。

- Phase 6-6 GitHubの既存branch更新拒否を2026-09-05、基準`a7ec228`（productionは`e2ce4a9`と同一）で確認しました。smoke PR #4の専用branchだけへ、local commit objectを使ったfast-forwardとnon-fast-forward更新を試し、両方がbroker内で拒否されました。検証用のreceive RPC停止guardへの到達は0回、auditはreceive-pack denied 2件、前後の専用branch／mainのremote OIDは一致しました。guardは到達時に実書き込みを防ぐためのもので、guardによる拒否をbrokerの成功には数えていません。container／broker thread／run directoryの回収とaudit非露出も成功しました。shared branchへのpush、ref削除、tag pushは行っていません。

- Phase 6-6 egressのlocal preflightとして、2026-09-05に専用project `agent-container-smoke`の制限を有効化し、追加domainを`pypi.org`だけに設定しました。専用image `129ea06302ee`を指定したdoctorはnetwork-policyを含む必須checkがPASSしました。既定のmanaged domainsはCodex／Claudeとも空であり、agentの接続先discoveryと実サービス操作は`not run — 次のblockの個別承認前`です。制限は有効なまま保持し、無断disable／domain追加／runtime起動はしていません。

- Phase 6-6 GitHubの書き込みsmokeを2026-09-05に個別承認後、基準`8c9af92`（productionは`e2ce4a9`と同一）と専用image `129ea06302ee`で実施しました。既存workspaceと別の一時checkoutで準備した1行のfixture commit `d89877b84397ad019de876f7d7bafd4fafb3a9da`について、remoteのbranch未存在・base一致をbroker経由で確認後、新規branch `test/github-broker-smoke-20260905-phase66`へのpushが成功し、remote head一致を読み戻しました。提示済みtitle/bodyで[smoke PR #4](https://github.com/jj1xgo/agent-container-smoke/pull/4)を1件作成し、固定summaryのviewとchecksが成功しました。checksは0件（候補treeにworkflowなし）で、CI成功とは扱いません。auditはreceive-pack 1／upload-pack 2／pr-create 1／pr-view 1／pr-checks 1件、全てokで、固定fieldと実capability・PR title/bodyの非露出を確認しました。終了後にbroker thread／run directory／検証containerを回収しました。merge・branch削除・PR close・Issue作成・既存branch更新のnegative pushは実施していません。PRとbranchは検証成果として保持し、GitHub smoke全体は残存gateがあるためPARTIALです。

- Phase 6-6のFamily前提確認として、2026-09-05に既存bindingのlocal doctor（state／binding／pending／audit／App metadata permissions）が成功しました。続けて既存の`LiveFamilyInventory.resolve`で実installationを読み取り、selected repositoryがexactly 1件で既存bindingと一致することを確認しました。credential、repository名／ID、pending本文は記録せず、binding変更・Issue作成はしていません。実agentからのintakeと承認付きIssue作成は未実施で、Family smoke全体はPARTIALです。

- Phase 6-6 GitHubの承認済みread-only smokeを2026-09-05に実施しました。基準`3f264bd`（productionは`e2ce4a9`と同一）、専用image `129ea06302ee`、project `agent-container-smoke`で、実broker経由のfetch、既存open Issue 1件のlistとIssue #1の固定field付きviewが成功しました。通常の`run_codex_spec`のmountを使い、agent本体を固定Python probeへ置換した検証containerです。GitHub credential環境変数・host gh/App state mountがなく、broker mountがread-only、`gh auth status`が失敗し、読めるprocess環境／argvに検査対象のGitHub secret markerとcapabilityがないことを確認しました。Issue本文は記録していません。broker終了後の同containerからのfetch／Issue参照は失敗し、broker thread、run directoryと検証containerを回収、workspaceのHEADと作業差分は不変でした。auditはupload／list／view各1件・全てokで、固定fieldと各値のschema検証が成功しました。最初のaudit非露出probeはcloseで空文字へ消去済みのcapabilityを比較して誤判定したため、その結果は無効です。外部操作は再実行せず保存済みauditを厳密検証しましたが、元capabilityとの直接比較は`not run — cleanupで消去済み`です。Appのlive installation／permission inventory、PR除外sentinelの照合、認証済みagent本体の実行、push／PR作成は今回の観測に含めず、GitHub smoke全体はPARTIALです。

- Phase 6-6の実Podman gateは、基準`e2ce4a9`とproduction／image入力が同一の専用worktree（文書・期待件数更新commit `9d8f5b3`）で専用imageをbuildし、14 tests／skip 0／ResourceWarning 0で成功しました（103.907秒）。image IDは`129ea06302ee495ac5c5f30cc3a036c645076b80b9b0db1d9f2626e9cbf6cc4a`、Node v26.8.1、Codex 0.153.4、Claude 2.1.261です。既存手順の専用imageを`AGENT_CONTAINER_INTEGRATION_BASE_IMAGE`にも指定し、egress／派生image testも同じbuildを使用しました。実行後は開始前からのcontainer 1件だけが稼働し、テスト用containerは残っていません。認証済み実host smokeは引き続き`not run — 対象を明示した個別承認前`で、6-6全体は未完了です。

- Phase 6-6の準備として、2026-09-05に基準`e2ce4a9`のhost上でlint、Codex 48件、container 1111件、broker socket 18件、forced-unknown 4件、whitespace検査が成功しました。最終実行はskipなし、socketのResourceWarningは0件です。containerの初回実行はsandboxのsocket制限で16 errors／1 skipとなり、host権限での再実行は成功しました。Podman 5.8.6／rootless／crunを確認済みですが、この準備確認時点では実Podman suiteと認証済み実host smokeは`not run`でした。実Podmanの後続結果は前項に記録します。Family手順は利用者承認により固定期待件数だけを更新し、操作・検証項目と過去の観測記録を維持します。

- Phase 6-5のproduction／test commit `a65704b1c64681064d2602e0a31e57c52da3ba3b`で、container 1,111件、Codex 48件、broker socket 18件、forced-unknown 4件、lintとwhitespace検査がPASSしました。ResourceWarningは0件、既存test/support/fixture 88 filesと対象外source 62 filesは基準`39fbc5e`から不変、保持33関数とserve内側処理のAST一致、256 codec casesの値・例外一致、旧encoder/auditからのstatic golden再生成一致を確認しました。local Podmanは`not run — podman unavailable`、required CIはPRで実行、認証済み実host smokeは`not run — 6-6`です。

- Phase 6-4の最終production／test tree `4e330826f3c04d5058ea54d7d65866945ebd8ca7`で、`bin/lint`、container unit 1,097件、Codex unit 48件、docs unit 67件、GitHub／handover／egress socket integration 8件がPASSし、socket stderrの`ResourceWarning`は0件でした。基準commit `a69bb780dc61f3f0f50c92f668f8686837280f12`から既存test 82 filesとscope外production 7 filesが不変であることをmachine-checkし、同commitから独立再生成したGitHub golden fixture（6,422 bytes、SHA-256 `398db7fe64dd10fa622a54da987ad7e6750c10e3a52ebe92ccd7dbbe5403eb49`）および90 synthetic protocol casesの一致も確認済みです。local Podman 14-test gateは`not run — podman unavailable`、required CIは`not run — PR作成後にcontrollerが実行`、実host smokeは`not run — 6-6`です。Phase 6は引き続き進行中で、stage 2のlifecycle／capability／audit opener完全統一が残っています。

## [0.5.0] - 2026-09-05

### Added

- project単位のexact-domain egress allowlistを追加しました。許可したdomainへのHTTPS CONNECTだけをhost側gatewayで仲介し、DNS／宛先IPの検査と監査を行います。

- 開発用Appとは権限・installation・stateを共有しないfamily専用GitHub Appと、hostのrequest単位承認後だけ登録済みrepositoryへIssueを1件作成するfamily Issue brokerを追加しました。
- Codex／Claudeのcredential-free intake、24時間／10件limit、canonical preview、unknown reconciliation、content-free audit、doctor、実Podman／実host smoke手順を追加しました。
- Family Issue本文へ、hostが選択したCodex／Claudeと登録済み提出元repository名から生成する改変不能な署名を追加しました。
- ホストのCodexとClaude Codeから、現在のGit originと登録済みproject metadataだけで保存先を決めるstandalone host handover publisherを追加しました。Claude Code向けには、最新handoverのpathだけを通知するSessionStart hookとhost skillを`profiles/host-claude/`へ追加し、publisherは`CODEX_SESSION_ID`がない場合に`CLAUDE_SESSION_ID`をSession欄へ記録します。

### Fixed

- 旧egress runtimeでも、登録済み・開始前workerと停止処理の競合を再現し、PR #99と同じjoin guardを適用しました。停止未完了時にcapabilityを失効し、開始成功／失敗後の再試行でworkerとruntimeを回収します。

- `bin/agentctl auth claude`のtoken validatorが印字可能ASCIIなら何でも受理していたため、browserに表示される`code#state`形式のlogin codeを`claude setup-token`のtokenとして保存でき、local `claude auth status`とdoctorがPASSしたまま実inferenceがHTTP 401になっていました。validatorを`sk-ant-oatNN-` prefixと英数・`-`・`_`に限定し、`#`を含む値はlogin codeの貼り間違いとして拒否します。既存の不正な保存値はdoctorの`claude-auth`がFAILとして報告します。
- terminalの幅で2行に折り返されたtokenを貼り付けると、hidden promptが1行目だけを保存し、2行目がagentctl終了後にshellへ渡ってhistoryに残っていました。hidden promptはgetpassを使わずechoとcanonical modeを切ってterminalを直接読み、Enterの後も入力が静止するまで読み続けて行を連結し、連結後も不正な場合は折り返しの可能性を示して停止します。

### Security boundaries

- containerへはrun限定socketとone-time capabilityだけを渡し、family App key／token、repository、pending host path、approval commandを渡しません。作成済みIssueのedit、close、deleteやfailure時fallbackは提供しません。

### Validation

- 2026-09-02の実host smokeでlocal automated gate、Codex／Claude両pathの実Podman gate、専用Appの最小権限とexact 1 repository inventory、Codex intake／duplicate拒否、fresh approval付き[Issue #75](https://github.com/jj1xgo/agent-container/issues/75)、content-free audit、terminal cleanupを確認しました。当時HTTP 401で停止していたClaude実CLI intakeは、原因がsetup tokenの貼り間違いだったため、validator強化後の2026-09-04に再実行し、実Claude runtimeからの固定fixture提出、同runの2回目拒否、host previewでのClaude署名、audit／cleanupをGitHub Issueを作らずに確認しました。これでPhase 5の実host smokeは完了です。

## [0.4.1] - 2026-09-01

### Fixed

- `v0.4.0`は`v0.3.0`のresolver baseを保持していたため、公開済みtagはimmutableのまま維持し、`v0.4.1`でexact-tag outputを修正しました。

## [0.4.0] - 2026-08-29

### Added

- 新規GitHub broker projectへ`--github-repository-id`を明示するproject-scoped repository bindingと、local doctorでexplicit binding／legacy global fallbackを区別する診断を追加しました。
- 選択中repositoryのIssue list/view read-only interfaceを追加しました。固定schemaでopen Issue一覧とopen／closed Issue詳細を返し、Pull Requestと除外fieldを応答から外します。
- private fixture repositoryを使うPhase 4 smoke gateを追加し、create-only Git、Issue read、runtime cleanup、stale client拒否をcredential-freeなbounded evidenceで確認できるようにしました。

### Changed

- GitHub broker pushをcreate-onlyへ変更しました。advertisementに存在しないunprotectedなbranchをold OID zeroで作成する場合だけ許可し、fast-forwardを含む既存branchへのupdateを拒否します。追加作業は新しいbranchと、必要に応じて新しいPRを使います。
- Git 2.53のupload-pack／receive-pack framingとdelete-only request終端へ対応し、terminal flushやopen client pipeでbroker接続が停止しないようにしました。
- Phase 4でREADME、初期設計、operator guideのscopeを整合し、shipped interfaceを選択中repositoryのread-only操作へ限定しました。
- 新しいproject policyからruleset markerと登録時のruleset確認optionを削除しました。旧exact true-marker schemaはcompatibility inputとしてだけ読み取ります。local doctorは有料のGitHub branch settingを確認済みとは表示しません。
- Phase 4 smokeの`upload-discovery`失敗は、global App metadataのrepository IDとsmoke repositoryの不一致が原因でした。project policyへrepository IDを限定し、既存旧schema policyだけはlegacy fallbackを維持します。
- operator/smoke手順へhost-only bounded REST `GH_CONFIG_DIR=... gh api repos/OWNER/REPOSITORY --jq .id` inventory、partial-state recovery gate、retry直前のfresh approvalを追加しました。GraphQL `gh repo view --json id`のnode IDはnumeric repository IDとして使いません。登録recoveryはfresh approval後に一度だけ実行し、後続のnegative gate失敗後は再試行していません。
- Phase 4 private smokeではruleset inventoryがHTTP 403のupgrade-or-public制限となり、修正前のunrelated-history force pushが受理されてdisposable remote branchが変更されました。このFAILを保持したうえで、修正版の実host gateは新しいbranchへの通常更新とunrelated-history更新をともに拒否し、remote不変を確認しました。Issue list/viewとstale-client cleanupも最終再実行でPASSしました。

### Security boundaries

- repository IDはproject policyからexactly one repositoryのtoken発行にだけ使い、broker audit、container output、container mountへ追加しません。doctorはlocal stateだけを検査し、remote App selection、permission、GitHub branch setting、networkを証明しません。
- family Issue create/commentは開発repository brokerと権限を共有しない将来Phaseへ延期します。外向き通信のdomain allowlistも未実装で、既知の`WARN network-policy`を維持します。

## [0.3.0] - 2026-08-28

### Added

- Claude Codeが選択projectに新規handoverを作成できる、runtime限定のcreate-only Unix-socket brokerと`agent-handover create --title TITLE`経路を追加しました。
- 7 section stdin contract、read-only mount、operator guide、merge後の認証済み実host smoke gateを追加しました。handover gateの全項目は2026-08-27にPASSしました。

### Security boundaries

- Claudeのhandover mountはread-onlyで、brokerはread、list、overwrite、rename、deleteを提供しません。failure時のdirect writerやread-write mountへのfallbackもありません。
- peer UID、runtime capability、固定projectでrequestを認証し、auditに本文、title、capability、credential由来情報を残しません。Codexの既存direct handover writerは変更しません。
- LinuxのClaude sandbox内からruntime限定handover brokerへ接続できるよう、managed policyでUnix socket syscallを許可します。hostの`/run`、`/var/run`、Podman socketはmountせず、到達範囲をproject別bind mountと明示的なhandover／optional GitHub broker runtimeに限定します。unsandboxed commandとdirect-write fallbackは引き続き禁止します。
- merge後の認証済みClaude実host smokeで、handover create、read-only直接変更拒否、cross-project拒否、malformed sectionとdummy credentialの拒否、検査sentinel・title・body・capabilityを含まない固定output/audit、runtime終了後のcapability失効を確認しました。

## [0.2.0] - 2026-08-27

### Added

- `agentctl stats PROJECT`向けのproject／agent runtime label、secret-free resource snapshot、cross-agent review用の共通PR templateを追加しました。
- 新規projectへSuperpowersを標準導入し、Codexは`obra/superpowers`本家、Claude Codeは公式pluginを使うようにしました。通常runはproject別snapshotを維持し、`agentctl superpowers update PROJECT|--all-projects`でだけ明示更新します。
- 保存先とprojectを環境から固定する`agent-handover` wrapperと、その`create`操作だけを許可するCodex初期ruleを追加しました。
- 既存projectのcustom rulesを保ったままhandover用profileを更新する`agentctl project update-profile`を追加しました。
- GitHub認証、image build、Codex認証、project登録、診断を一度に案内する`bin/setup.sh`を追加しました。
- 新規Codex project stateへ、読み取り専用の`gh pr`、`gh issue`、`gh run`、`gh repo view`操作を事前許可する初期rulesを追加しました。
- GitHub App credentialをhost memory/private stateへ限定するproject-scoped brokerを追加しました。
- exact repositoryのclone/fetch、policy-gated work-branch push、固定schemaの`agent-github pr create/view/checks`をbroker経由で利用できます。
- broker runtime、doctor、project登録、実Unix socket CI、Phase 3運用ガイドと実host smoke checklistを追加しました。

### Security boundaries

- GitHub brokerは明示opt-inで、失敗時にlegacy `gh` credentialへfallbackしません。
- pushのnon-fast-forward拒否には、brokerのlease/ref gateに加えてGitHub側の全branch force-push禁止rulesetが必要です。
- 実GitHub App smokeでは、credential非露出、exact repositoryのclone／fetch、別repository拒否、通常の作業branch push、PR create／view／checks、secret-free auditとcleanupを確認しました。shared repositoryへ影響するprotected branch、delete、stale lease、non-fast-forwardのnegative pushは安全上実施していません。外向きnetworkは引き続きdomain allowlistされていません。

### Changed

- mainの開発versionを`v0.1.0`からのfirst-parent commit数と短縮SHAから`0.2.0-dev.N+gCOMMIT`として自動生成し、tracked変更があるcheckoutには`.dirty`を付けるようにしました。base imageにもbuild時のversionを埋め込みます。
- GitHub brokerのephemeral runtime pathを短縮し、標準のhost state配置でもUnix socketのpath長上限を超えないようにしました。
- GitHub App tokenの要求・response検証へ暗黙の`Metadata: read`権限を明示し、GitHubの実responseを厳密な最小権限のまま受理するようにしました。
- GitHub upload-pack discoveryのSmart HTTP service preambleを厳密に検証・除去し、protocol v2 advertisementをbroker clientへ渡すようにしました。
- broker clone URLの`insteadOf`を`.git` suffixまでexact matchさせ、Smart HTTPのflush packetをremote-helper用response-endへ変換するようにしました。
- receive-pack remote-helperをGitの`connect` negotiationへ合わせ、実Git 2.53がNUL直後へ置くcapability区切りspaceを厳密に受理するようにしました。
- GitHub brokerの既知connection failureをsecret-freeな固定stageでauditし、1 connectionの失敗でbroker accept loop全体を停止しないようにしました。
- handover名をUTC・秒精度・一意suffixにし、timezone付き`Created`実時刻で最新を選ぶようにしました。host/containerの並行sessionがlocal時刻のファイル名で誤順序になりません。
- Codex runtimeを`--approve-for-me`付きで起動し、内側のworkspace-write sandboxを維持しながらapproval requestを自動reviewするようにしました。完全なapproval／sandbox bypassは有効にしません。
- Codex status lineから累積token数を外し、model、context、利用枠、Git branch、project名に絞りました。

## [0.1.0] - 2026-08-25

最初の公開releaseです。

### Added

- Linuxとrootless Podman向けのCodex・Claude Code分離runtime。
- project別workspace、agent設定、session、cache、handover境界。
- 共有credentialを限定する認証flowとsecret-freeなdoctor出力。
- Claude Enterprise managed policy、weaker nested sandbox、fail-closed security probe。
- project別derived image、package設定、agent Nodeとproject Nodeの分離。
- Debian package取得の署名検証付きCA bootstrapと、その後のHTTPS強制。
- 通常testと実Podman統合testを分離したGitHub Actions CI。

### Security boundaries

- rootless Podman、read-only runtime、capability削除、no-new-privileges、狭いmountを維持します。
- Claude hooksとMCPは初期状態で無効です。stdio MCPは未対応です。
- 外向きnetworkはdomain allowlistされていません。
- credentialや実inferenceを使うhost smoke testは、CIの自動testには含まれません。

### CI validation baseline

- Node.js `26.7.0`
- Codex CLI `0.149.1`
- Claude Code `2.1.243`

通常のlocal image buildは既定で各agent CLIの`latest`を解決します。このbaselineは`v0.1.0`のCI再現用固定値であり、runtime dependencyを恒久固定するものではありません。

[0.5.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.5.0
[0.4.1]: https://github.com/jj1xgo/agent-container/releases/tag/v0.4.1
[0.4.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.4.0
[0.3.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.3.0
[0.2.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.2.0
[0.1.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.1.0
