# Debian testing・プロジェクト派生イメージ・Claude nested sandbox設計

日付: 2026-08-24

## 背景

Phase 2のClaude setup-token対応では、専用launcherがtoken fileを読み、Claude親processだけへ`CLAUDE_CODE_OAUTH_TOKEN`を渡し、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`で全subprocessから資格情報を除去する設計だった。しかしClaude Code 2.1.241では、global scrubがLinuxの強いbubblewrap sandboxを強制し、`enableWeakerNestedSandbox`の指定を無効化する。rootless Podman内では新しい`/proc`をmountできないため、ClaudeのBash toolは`bwrap: Can't mount proc on /newroot/proc: Operation not permitted`で起動できない。

同時に、今後`findsummits`と`sotlas-frontend`をCodex/Claudeの両方で扱うため、共通imageをDebian testingへ揃え、プロジェクトごとのAPT packageとNode.js versionを安全かつ再現可能に追加できる仕組みが必要になった。

本設計は、既存のsetup-token設計のtoken保管・mount・launcher部分を維持しつつ、global scrubとClaude sandboxの組み合わせを置き換える。また、共通base imageとproject derived imageを分離する。

## 目標

- 共通base OSを`debian:testing-slim`にする。
- Codex/Claude用Node.jsは公式nodejs.org配布物を使う。
- 通常のbase image rebuildではCodex、Claude Code、agent runtime Nodeの最新安定版を解決する。version指定は再現・rollback用の明示的なoverrideに限定する。
- project固有のAPT packageとNode.js versionを`.agent-container.d/`から宣言し、必要なderived imageを自動build・reuseする。
- agent runtime Nodeとproject Nodeを分離し、project pinでCodex/Claudeを壊さない。
- rootless Podmanの外側sandboxを維持しながら、ClaudeのBash toolをnested container内で利用可能にする。
- ClaudeのOAuth tokenをBash環境、Bashから読めるfile、built-in Read、`/proc`経由で取得できないことを、秘密値を表示せず検証する。
- global scrubを外すことで保護対象外になるcommand hooksとstdio MCPをfail closedで停止する。

## 非目標

- `.claude-container.d/`との互換性。
- projectが任意のDockerfile、Containerfile、shell script、APT optionを注入する仕組み。
- container実行中にroot権限でpackageを追加する仕組み。
- 初期導入時のClaude command hooks、stdio MCP、未審査HTTP MCPの利用。
- rootless Podman内でClaudeのstrong sandbox用fresh procfsを実現すること。
- Anthropic側の未確定な修正時期を前提にすること。

## 比較した方式

### 1. Outer Podman + weaker nested sandbox + scoped credential policy（採用）

外側のrootless Podmanを主要なisolation境界として維持し、Claude内では`enableWeakerNestedSandbox`を有効にする。BashにはClaudeのcredential policyを適用し、global scrubでしか保護できないcommand hooksとstdio MCPは停止する。

現在のhost制約でBashを使え、防御も可能な限り残せる。ただしcontainerの既存`/proc`がinner sandboxへ見えるため、親Claude processのenvironmentを読めないことをlive smokeで確認できなければ採用できない。

### 2. Global scrub + strong nested sandbox

全subprocessの資格情報除去とfresh PID/proc namespaceを維持できるが、現在のrootless Podmanではprocfs mountが拒否される。Anthropicまたはruntime側の変更がない限り実用にならない。

### 3. Claude内sandboxを無効化

Bashは動くが、filesystem・network制限が外側containerだけになる。global scrubの現在の実装はsandbox無効化も上書きするため、単純なfallbackにもならない。防御低下が大きいため採用しない。

## 全体architecture

imageを2層に分ける。

1. **共通base image**: Debian testing、agent runtime Node、Codex、Claude Code、共通tool、managed Claude policyを含む。
2. **project derived image**: 共通baseを継承し、検証済み`.agent-container.d/`のAPT packageとproject Nodeだけを追加する。

`agentctl build`は共通baseをbuildする。`agentctl run <project>`はproject設定を検査し、必要なderived imageを選択またはbuildしてから既存のruntime mountとsecurity optionを組み立てる。`agentctl doctor <project>`は同じ検査とimage選択判定をread-onlyで行い、missingまたはstaleなら次回`run`でbuildされることを報告する。project設定が空なら共通baseを直接使う。

runtimeでは引き続き次を維持する。

- rootless Podman
- non-root、keep-id
- read-only root filesystem
- `--cap-drop=all`
- `no-new-privileges`
- 必要なworkspace、agent state、cache、GitHub config、handover、token fileだけの限定mount
- Podman socket非mount

## 共通base image

baseは`debian:testing-slim`とする。不足packageは実際のbuild/testで判明した時点で、必要性を説明できるものだけ共通baseへ追加する。project固有dependencyは共通baseへ混ぜない。

agent runtime Nodeはnodejs.orgの公式Linux binary archiveから導入し、公開checksumと照合する。CodexとClaude Codeもagent runtime Node配下へinstallする。通常buildは`latest`を意味し、cachebusterを使う明示的なrebuildでは最新versionを解決する。既存の`--codex-version`と`--claude-version`に加え、agent runtime Nodeも明示versionへ固定できる入力を用意するが、defaultはlatest stableとする。

agent CLIのentrypoint wrapperは`/opt/agent-node/bin/node`とinstall済みCLI entrypointを明示して起動する。`/usr/bin/env node`やruntime `PATH`へ依存させない。

## Project configuration

canonical directoryはworkspace rootの`.agent-container.d/`だけとする。`.claude-container.d/`は検出して移行せず、設定として扱わない。曖昧な併用を避けるため、存在時はdoctorで非対応と明示する。

許可するfileは次の2つだけである。

- `packages.txt`: 1行1個のDebian package specification。空行と`#`から始まるcommentを許可する。
- `node-version.txt`: 公式Node.js releaseの厳密なversion番号1個。

directory、file、親directoryのsymlink、特殊file、path traversal、追加fileは拒否する。`packages.txt`ではleading option、whitespaceを含む1行、shell metacharacter、command substitutionを拒否する。package名と、必要な場合に限りDebianの`name=version`形式を許可する。buildは`apt-get update`後に`--no-install-recommends`でinstallし、APT metadataを削除する。

`node-version.txt`のarchiveはnodejs.org公式配布元だけから取得し、公式checksumと照合する。project Nodeは`/opt/project-node`へ置く。対話的なBashとproject commandでは`/opt/project-node/bin`をagent Nodeより先に`PATH`へ置くが、Codex/Claude wrapperは常にagent Nodeを直接使う。

`findsummits`で確認されたNode 22.14.0はCodex MCPをnpm installする目的で追加されたもので、project自体のNode依存は確認できなかった。そのため現時点ではproject Nodeを指定しない。`sotlas-frontend`は上流要件に従い、`.agent-container.d/node-version.txt`でversionを指定する。

## Derived image selection and cache

derived image keyは少なくとも次をcanonical serializationしてhash化する。

- 共通baseのimmutable image ID
- derived-image schema version
- 正規化したpackage list
- project Node versionまたは未指定
- target architecture

project名は表示とtag prefixだけに使い、cache identityは内容hashで決める。同じbase・設定・architectureなら既存imageをreuseする。base rebuild、設定変更、schema変更、architecture変更では新しいhashとなり、自動buildする。

build contextは検証済み設定からagentctlが生成する最小contextに限定し、workspace全体、Git metadata、agent state、token、session、handoverを含めない。project提供のDockerfileやscriptは実行しない。

build失敗時は、古い設定のimageへ黙ってfallbackしない。`run`は停止し、原因をsecret-freeなmessageで報告する。不要になったderived imageの自動削除は本sliceに含めず、将来の明示的なmaintenance commandへ分ける。

## Claude sandbox and credential policy

image-local launcherはtoken fileの`O_NOFOLLOW` open、通常file・permission・format検証、親Claude processへの`CLAUDE_CODE_OAUTH_TOKEN`設定、値を出力しない性質を維持する。一方、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`は設定しない。

Linuxのfile-based managed settingsである`/etc/claude-code/managed-settings.json`へ、少なくとも次のpolicyを置く。image内のread-only root filesystemに含め、workspaceやuser settingsから上書きできないようにする。

- `sandbox.enabled=true`
- `sandbox.enableWeakerNestedSandbox=true`
- sandbox unavailable時のunsandboxed fallbackを禁止
- sandbox初期化失敗時はfail closed
- `sandbox.credentials.envVars`で`CLAUDE_CODE_OAUTH_TOKEN`、Anthropic credential、利用し得るcloud provider credentialをdeny
- `sandbox.credentials.files`で`/run/secrets/claude-oauth-token`をdeny
- managed permission ruleでbuilt-in `Read`からtoken pathをdeny
- permission bypass modeをdisable
- `disableAllHooks=true`
- managed hook以外をblockし、初期状態ではmanaged hookも定義しない
- managed MCPだけを許可し、初期状態のmanaged MCP mapを空にする

これにより、command、prompt、agent、HTTP、MCP toolを含むClaude hookは初期状態ですべて動かさない。stdio MCPを含むMCPも初期状態ですべて動かさない。通常のRead/Edit/Write、sandboxed Bash、build/test、Git、package manager、compilerは利用できる。

HTTP MCPを追加する場合は別のsecurity reviewを行う。managed MCPへ審査済みserverだけを登録し、allowlistはuser-assigned nameではなく限定した`serverUrl`で照合する。stdio commandはglobal scrubを安全に戻せるまで許可しない。

## Security acceptance gate

weaker nested sandboxはcontainerの既存`/proc`をbindするため、fresh PID namespace相当のprocess情報隠蔽を提供しない。次のlive testをすべて満たすことを採用条件とする。

- sandboxed Bash childで`CLAUDE_CODE_OAUTH_TOKEN`が存在しない。
- sandboxed Bash childから`/run/secrets/claude-oauth-token`を読めない。
- Claude built-in Readからtoken fileを読めない。
- sandboxed Bash childから`/proc`経由で親Claude processのtokenを読めない。
- Claudeのsandboxが実際に有効で、unsandboxed fallbackが無効である。
- 通常のworkspace編集、build、test、Git操作が動く。

検査は専用の安全なprobeで行い、出力は各条件のtrue/falseだけとする。environment一覧、token値、prefix、suffix、長さ、hash、process environment、secret file本文をstdout、stderr、log、test artifactへ出さない。特にparent tokenの`/proc`検査が失敗した場合、推奨方式を採用せずPhase 2 live smokeを停止する。外側containerのcapability追加、privileged mode、host PID namespace共有をfallbackにしない。

## Error handling and doctor

次を起動前errorとして扱う。

- 共通base imageが存在しない。
- project configが非canonical、symlink、特殊file、不明fileを含む。
- package specificationまたはNode versionが不正。
- derived image build、download、checksum照合に失敗する。
- managed Claude policyがimageに存在しない、schema不正、期待値と異なる。
- hooksまたはMCPがpolicy上有効になっている。
- sandboxが利用不能、またはunsandboxed fallbackが可能である。
- auth、workspace、origin、state permissionなど既存preflightが失敗する。

`doctor`はbase image ID、project configの有無、derived imageのcurrent/stale/missing、agent/project Node version、managed policyの有効性をsecret-freeに表示する。`doctor`自身はimageをbuildしない。`run`が自動buildを行う場合は開始理由と結果を表示するが、credentialやproject file本文は表示しない。

## Testing strategy

### Unit tests

- project configの正常系、comment/空行、重複の正規化。
- unknown file、symlink、special file、traversal、shell構文、leading option、不正versionの拒否。
- base image IDと設定内容によるdeterministic hash。
- cache hit、base/config/schema/architecture変更時のcache miss。
- minimal build contextにworkspaceやsecretが入らないこと。
- project NodeのPATHとagent wrapperのNode分離。
- managed Claude settingsの必須policy。
- Podman runtime flagsとmountの既存security invariant。

### Container integration tests

- base OSがDebian testingであること。
- agent runtime Nodeが公式distributionで、Codex/Claudeが起動すること。
- default rebuildとexplicit version overrideの両方。
- sample packageとproject Nodeを含むderived imageのbuild・reuse・rebuild。
- runtimeがnon-root、read-only、capabilityなし、no-new-privilegesであること。
- Codexの既存sandbox・auth・handover flowが退行しないこと。

### Live smoke tests

- Claude setup-token authとTUI起動。
- managed settingsとsandbox状態の確認。
- security acceptance gateのboolean probe。
- workspaceの安全なfixtureでRead/Edit/Write、Bash、build/test、Git status。
- hooksとMCPが読み込まれないこと。
- Codexのversion、Bash、workspace edit、handover、session継続の回帰確認。

## Documentation and operations

operator向け資料へ次を明記する。

- 通常rebuildはagent CLIとagent runtime Nodeの最新安定版を取得すること。
- reproducibility/rollback時だけversion overrideを使うこと。
- `.agent-container.d/`の許可file、例、変更後の自動rebuild。
- runtime中にpackageを追加せず、宣言を変更して再起動すること。
- Claudeでは当面hooksとMCPを利用できないこと。
- HTTP MCP追加はmanaged policyのreviewが必要なこと。
- Anthropicがscrubとweaker nested sandboxの共存を修正した場合に再検証すること。
- security acceptance gateが失敗したら運用を継続しないこと。

## Existing designとの関係

`2026-08-23-phase-2-claude-setup-token-design.md`のうち、tokenのhost保存、permission検証、read-only secret mount、image-local launcher、argv/log非露出は維持する。`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`の必須化、全subprocessへのscrub保証、fresh PID namespaceの受け入れ条件は本設計で置き換える。

既存のPhase 2実装計画と運用文書でglobal scrubを前提とする箇所も、本設計の実装時に更新する。既存の未コミット文書変更は保持し、内容を確認したうえで競合しない差分として修正する。

## Completion criteria

1. 共通baseをDebian testing slimと公式agent runtime Nodeでbuildできる。
2. default buildが最新agent CLIを解決し、明示version overrideも機能する。
3. `.agent-container.d/`の安全なsubsetからderived imageを自動build・reuseできる。
4. project NodeがCodex/Claudeのruntime Nodeへ影響しない。
5. Claude Bashがrootless Podman内でweaker nested sandboxを使って動く。
6. managed policyをuser/project設定で緩められない。
7. security acceptance gateがすべてpassする。
8. hooks、stdio MCP、未審査HTTP MCPが起動しない。
9. unit、container integration、Claude live smoke、Codex regression smokeがpassする。
10. 制約と復旧条件が運用文書とhandoverに記録される。
