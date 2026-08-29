# Changelog

このprojectは[Semantic Versioning](https://semver.org/)に従います。`0.x`の間は、CLI、state配置、security boundaryが互換性なく変更される可能性があります。

## [Unreleased]

### Added

- 新規GitHub broker projectへ`--github-repository-id`を明示するproject-scoped repository bindingと、local doctorでexplicit binding／legacy global fallbackを区別する診断を追加しました。

### Changed

- GitHub broker pushをcreate-onlyへ変更しました。advertisementに存在しないunprotectedなbranchをold OID zeroで作成する場合だけ許可し、fast-forwardを含む既存branchへのupdateを拒否します。追加作業は新しいbranchと、必要に応じて新しいPRを使います。
- 新しいproject policyからruleset markerと登録時のruleset確認optionを削除しました。旧exact true-marker schemaはcompatibility inputとしてだけ読み取ります。local doctorは有料のGitHub branch settingを確認済みとは表示しません。
- Phase 4 smokeの`upload-discovery`失敗は、global App metadataのrepository IDとsmoke repositoryの不一致が原因でした。project policyへrepository IDを限定し、既存旧schema policyだけはlegacy fallbackを維持します。
- operator/smoke手順へhost-only bounded REST `GH_CONFIG_DIR=... gh api repos/OWNER/REPOSITORY --jq .id` inventory、partial-state recovery gate、retry直前のfresh approvalを追加しました。GraphQL `gh repo view --json id`のnode IDはnumeric repository IDとして使いません。登録recoveryはfresh approval後に一度だけ実行し、後続のnegative gate失敗後は再試行していません。
- Phase 4 private smokeではruleset inventoryがHTTP 403のupgrade-or-public制限となり、unrelated-history force pushが受理されてdisposable remote branchが変更されました。runtime内の最終OID check前に停止し、別のbounded host observationで変更を確認しました。retry、復元、PR、Issue、cleanup、releaseは実施していません。

### Security boundaries

- repository IDはproject policyからexactly one repositoryのtoken発行にだけ使い、broker audit、container output、container mountへ追加しません。doctorはlocal stateだけを検査し、remote App selection、permission、GitHub branch setting、networkを証明しません。

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

[0.3.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.3.0
[0.2.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.2.0
[0.1.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.1.0
