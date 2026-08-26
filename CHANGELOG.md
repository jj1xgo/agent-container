# Changelog

このprojectは[Semantic Versioning](https://semver.org/)に従います。`0.x`の間は、CLI、state配置、security boundaryが互換性なく変更される可能性があります。

## [Unreleased]

### Added

- 新規Codex project stateへ、読み取り専用の`gh pr`、`gh issue`、`gh run`、`gh repo view`操作を事前許可する初期rulesを追加しました。
- GitHub App credentialをhost memory/private stateへ限定するproject-scoped brokerを追加しました。
- exact repositoryのclone/fetch、policy-gated work-branch push、固定schemaの`agent-github pr create/view/checks`をbroker経由で利用できます。
- broker runtime、doctor、project登録、実Unix socket CI、Phase 3運用ガイドと実host smoke checklistを追加しました。

### Security boundaries

- GitHub brokerは明示opt-inで、失敗時にlegacy `gh` credentialへfallbackしません。
- pushのnon-fast-forward拒否には、brokerのlease/ref gateに加えてGitHub側の全branch force-push禁止rulesetが必要です。
- 実GitHub App smoke gateは未実施で、外向きnetworkは引き続きdomain allowlistされていません。

### Changed

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
