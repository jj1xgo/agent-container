# Changelog

このprojectは[Semantic Versioning](https://semver.org/)に従います。`0.x`の間は、CLI、state配置、security boundaryが互換性なく変更される可能性があります。

## [Unreleased]

### Added

- 開発用Appとは権限・installation・stateを共有しないfamily専用GitHub Appと、hostのrequest単位承認後だけ登録済みrepositoryへIssueを1件作成するfamily Issue brokerを追加しました。
- Codex／Claudeのcredential-free intake、24時間／10件limit、canonical preview、unknown reconciliation、content-free audit、doctor、実Podman／実host smoke手順を追加しました。
- Family Issue本文へ、hostが選択したCodex／Claudeと登録済み提出元repository名から生成する改変不能な署名を追加しました。
- ホストのCodexとClaude Codeから、現在のGit originと登録済みproject metadataだけで保存先を決めるstandalone host handover publisherを追加しました。Claude Code向けには、最新handoverのpathだけを通知するSessionStart hookとhost skillを`profiles/host-claude/`へ追加し、publisherは`CODEX_SESSION_ID`がない場合に`CLAUDE_SESSION_ID`をSession欄へ記録します。
- 標準agent imageに`jq`を追加しました。agentがJSON出力(`.claude.json`、CLI応答など)を扱う際、代替commandを自作せず本物の`jq`を使えます。

### Fixed

- Claude launcherが設定する`IS_DEMO=1`はtoken onboardingだけでなくworkspace trust dialogも省略するため、project configの`.claude.json`に`hasTrustDialogAccepted`が残らず、managed status lineを含むtrust前提の機能が黙って動いていませんでした。launcherは起動直前に現在のworkspaceの該当keyだけをseedし、fileが無ければmode `0600`で作り、通常fileでない・実行user所有でない・JSON objectとして読めない場合は本文を出さずに起動を停止します。permission bypass optionやproject側hooks／MCPの扱いは変わりませんが、trust承認と同じくworkspace内`.claude/settings*.json`の`permissions.allow`と`additionalDirectories`は有効になります。
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

[0.4.1]: https://github.com/jj1xgo/agent-container/releases/tag/v0.4.1
[0.4.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.4.0
[0.3.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.3.0
[0.2.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.2.0
[0.1.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.1.0
