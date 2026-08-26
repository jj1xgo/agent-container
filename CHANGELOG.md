# Changelog

このprojectは[Semantic Versioning](https://semver.org/)に従います。`0.x`の間は、CLI、state配置、security boundaryが互換性なく変更される可能性があります。

## [Unreleased]

### Added

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
- 実GitHub App smoke gateは未実施で、外向きnetworkは引き続きdomain allowlistされていません。

### Changed

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

[0.1.0]: https://github.com/jj1xgo/agent-container/releases/tag/v0.1.0
