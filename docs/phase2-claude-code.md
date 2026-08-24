# Phase 2 Claude Code 運用ガイド

Phase 2では、既存のrootless Podmanとproject別workspaceを維持したまま、CodexとClaude Codeを同じimageから選択して実行します。設計上の境界は[Phase 2設計](superpowers/specs/2026-08-23-phase-2-claude-code-design.md)を参照してください。

## 開始前: 前提条件とnetwork WARN

- Linux上のrootless Podmanが必要です。host network、Podman socket、通常のhost workspace、他projectのhandoverはcontainerへ渡しません。
- 状態rootは既定で`${XDG_DATA_HOME:-~/.local/share}/agent-container`です。必要なら、実在する絶対pathを`AGENT_CONTAINER_HOME`へ設定します。state directoryは`0700`、credential fileは`0600`、いずれも実行user所有で、symlinkは使えません。
- hostの`~/.claude`はmountしません。Claudeの共有認証は状態rootの`shared-auth/claude/oauth-token`だけです。directoryは`0700`、token fileは通常file・`0600`・非symlink・実行user所有でなければなりません。credential本文を表示しません。
- Phase 2では`外向き通信はドメイン制限されていません`。package取得、Claude、GitHubへ通常のrootless Podman networkで接続します。domain allowlist済みとは扱いません。
- GitHub CLI（`gh`）認証は専用state rootの`gh`へあらかじめ用意します。認証更新やscope変更は通常runの外で行います。

以後の例では状態rootを明示します。

```bash
export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"
```

## Image build、更新、rollback

通常buildは`debian:testing-slim`をbaseにし、agent用Node.jsをnodejs.orgの公式配布物から`latest`で取得してchecksumを検証します。CodexとClaude Codeもnpmから`latest`でinstallします。3つの既定値が`latest`であり、毎回新しいcachebusterをbuild argumentへ渡してCLI install layerを無効化するため、通常buildのたびに現在公開されている最新versionを解決します。build後は出力されたNode、Codex、Claude Codeのversionを記録します。runtimeのself-updateは`DISABLE_UPDATES=1`で無効です。version変更はimageの再build時だけに起き、実行中のcontainerでversionは変わりません。

```bash
bin/agentctl build
```

問題調査またはrollbackでだけ、既知の安全なversionを明示してimageを再buildします。通常の更新に固定version optionを使いません。versionは空白、control character、optionに見える値を受け付けません。

```bash
bin/agentctl build \
  --node-version VERSION \
  --codex-version VERSION \
  --claude-version VERSION
```

build contextにcredentialや状態rootは入れません。buildはnetwork accessと現在のpackage取得を伴うため、実hostでの実行前に利用者承認を得ます。

## Claude setup-token認証

初回操作は次の順で行います。

```bash
bin/agentctl build
bin/agentctl auth claude
bin/agentctl doctor agent-container --agent claude
bin/agentctl run agent-container --agent claude
```

`bin/agentctl auth claude`は必ず利用者本人がscreen recordingやcaptured automationのないprivate terminalで実行します。内部で起動する`claude setup-token`は、Claude subscriptionで使う1年間有効なinference専用tokenをそのterminalに一度表示します。その後のhost promptは入力を表示しません。表示されたtokenはそのhidden promptにだけ貼り付け、chat、この会話、handover、shell history、screenshot、log、command line、環境変数へ貼り付けたり記録したりしません。このcredentialではClaude Remote Controlを利用できません。

```bash
bin/agentctl auth claude
```

setup containerはhostのClaude configをmountせず、tmpfs上の一時`CLAUDE_CONFIG_DIR`だけを使用します。hidden promptで受け取ったtokenは同じprivate directoryのstaging fileへ保存され、そのstaging tokenをread-only mountしたlauncher経由の`claude auth status`が成功した後だけ、`shared-auth/claude/oauth-token`へatomic replaceされます。setup、prompt、format、staging、status、activationのどこで失敗または取消になっても、それまで有効だったtokenは変更されず、legacy stateも移動しません。statusのstdout/stderrとtoken本文は表示・記録しません。

新tokenのactivationが成功した後だけ、旧shared authの`.credentials.json`、`.claude.json`、`backups`が`<state-root>/quarantine/claude/<run-id>/`へ移されます。quarantine directoryは`0700`、fileは`0600`に正規化され、symlinkや特殊fileは拒否されます。quarantineは自動削除も自動復元もしません。

## 初回Claude run前のmigration

対象projectは先にPhase 1と同じ安全なproject registrationで準備します。`--handover-root`には、対象project directoryを含む絶対pathを指定します。

```bash
bin/agentctl project add OWNER/REPOSITORY \
  --handover-root ABSOLUTE_HANDOVER_ROOT
```

旧Claude設定を移す場合、最初のClaude runより前に必ずdry-runをreviewしてからapplyします。移行元を自動検出しないため、旧projectのClaude config directoryを絶対pathで指定します。

```bash
bin/agentctl migrate claude PROJECT --from ABSOLUTE_OLD_CLAUDE_CONFIG
bin/agentctl migrate claude PROJECT --from ABSOLUTE_OLD_CLAUDE_CONFIG --apply
```

既定は`dry-run`で、destinationを変更しません。`CLAUDE.md`、`settings.json`、`agents/`、`commands/`、`rules/`、`skills/`、`hooks/`だけが候補です。`.credentials.json`、`.claude.json`、session/transcript/cache/log、handover、`.git`、allowlist外の内容はcopyしません。pluginsも既定では除外します。必要なら、移行元metadataに存在する完全一致identifierだけを明示してdry-runを再reviewします。

```bash
bin/agentctl migrate claude PROJECT --from ABSOLUTE_OLD_CLAUDE_CONFIG \
  --plugin NAME@MARKETPLACE
bin/agentctl migrate claude PROJECT --from ABSOLUTE_OLD_CLAUDE_CONFIG \
  --plugin NAME@MARKETPLACE --apply
```

`--apply`はdestinationの`claude-config`が存在したら失敗します。copyはstaging後のdirectory単位renameで公開され、sourceはread-only入力として扱います。dry-runの一覧とsourceをreviewしてからapplyしてください。credentialらしい`settings.json`は拒否されます。旧`claude-containerを変更しません`。旧container、旧state、旧Git repositoryはこのmigrationの対象外です。

## Doctor

agentを指定しない既存commandは、既定でCodexを診断します。

```bash
bin/agentctl doctor PROJECT
```

Claudeだけを診断するには次を実行します。

```bash
bin/agentctl doctor PROJECT --agent claude
```

CodexとClaudeの状態を一度に診断する場合は、共通checkを一回だけ実行してagent別checkを固定順で表示します。

```bash
bin/agentctl doctor PROJECT --agent all
```

`doctor`はrootless Podman、image、CLI version、private state、認証file、Claude config、専用`gh`設定、workspaceのHTTPS origin、project handover境界を診断します。Claudeでは`claude-auth`がtokenの通常file・owner・mode・format、`claude-auth-status`がlauncher経由のlocal status、`claude-config`がproject config directory、`claude-project-credentials`がproject config内の`.credentials.json`不在を表します。`WARN network-policy`は既知のnetwork制約であり、`FAIL`が一つでもあれば非zeroで終了します。出力は存在・mode・check結果だけで、credential本文や環境変数値を出しません。

`claude-auth-status`のPASSだけではtokenが現在のAPI inferenceに使える証明になりません。staleまたはrevoked tokenでもlocal statusが成功し、実inferenceがHTTP 401になる可能性があるため、host smokeでは必ず最小の実API応答も確認します。

## Claudeのrunとresume

診断の`FAIL`を解消してから、対象projectをClaudeで起動します。

```bash
bin/agentctl run PROJECT --agent claude
```

agent指定を省略した既存commandはCodexのままです。Codexへ戻すには次を使います。

```bash
bin/agentctl run PROJECT
bin/agentctl run PROJECT --agent codex
```

Claudeを通常終了した後は同じprojectを同じ`run --agent claude`で再起動し、Claude CLIのsession resume操作は対話画面で確認します。host上の`~/.claude`や他projectのClaude stateを参照してresumeしません。

## Claude runtimeの正確なmount

`bin/agentctl run PROJECT --agent claude`が渡すhost sourceは次だけです。

| Host source | Container target | Mode |
| --- | --- | --- |
| 選択projectのworkspace | `/workspace` | read-write |
| 選択projectの`claude-config` | `/home/agent/.claude` | read-write |
| 共有`oauth-token` | `/run/secrets/claude-oauth-token` | read-only file mount |
| 選択projectのcache | `/home/agent/.cache` | read-write |
| 専用`gh` config | `/home/agent/.config/gh` | read-only |
| 選択projectのhandover directory | `/handovers/PROJECT` | read-write |

containerは`--read-only`、`--cap-drop=all`、`no-new-privileges`、host userと一致するkeep-id namespace、bounded `/tmp` tmpfs、Linuxのcontainer PID namespaceを使います。Claude向けpermission bypass optionは渡しません。`CLAUDE_CONFIG_DIR=/home/agent/.claude`、`AGENT_PROJECT_ID`、`AGENT_HANDOVER_ROOT=/handovers`を設定します。

専用launcherだけがsecret fileを`O_NOFOLLOW`で開いてtokenをClaude processへ渡します。tokenはPodman argvにもhost環境にも入りません。launcherは`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を必須設定し、ClaudeのBash、hooks、MCP stdio serverなどのsubprocessへ`CLAUDE_CODE_OAUTH_TOKEN`やAnthropic/cloud credential環境変数を継承させません。確認時も環境一覧、値、prefix、長さ、process environment、secret file本文を表示せず、「存在しない」というbooleanだけを記録します。

## 障害時

- `doctor`のFAILは、表示されたcheckを起点に修正します。state mode、ownership、symlink、workspace origin、image、rootless設定を確認し、credential本文を読まないでください。
- credentialを調べるときは`lstat`相当でexact path、type、mode、ownerだけを確認します。`oauth-token`、旧`.credentials.json`、`.claude.json`、backupに対して`cat`、JSON parser、checksum、prefix確認、長さ表示など本文を読むcommandは実行しません。
- setup、hidden prompt、token format、`claude auth status`、activationのいずれかが失敗したら停止し、以前のactive tokenが維持され、legacy stateとquarantine対象が移動していないことをmetadataだけで確認します。共有config directory全体のmount、nested credential mount、projectごとのcredential copyへ緩和しません。
- project `claude-config/.credentials.json`が空fileを含め存在すれば、runtimeとdoctorは拒否します。認証成功後のrecoveryでだけ本文を読まずにprivate quarantineへ移し、project `.claude.json`、backups、cache、sessions、plugins、memoryは移動しません。
- migrationが拒否または衝突した場合は、destinationを手で消去・上書きせず、dry-runのsource boundary、allowlist、plugin metadataをreviewします。
- 実hostの対話login、credential refresh、resume、mount検査は[Phase 2 smoke checklist](phase2-smoke-test.md)で承認を得て実行します。未実施をPASSと扱いません。

## Codexへのrollbackと旧container

Claudeで問題が起きても、同じprojectを`bin/agentctl run PROJECT --agent codex`でCodexとして起動できます。CLI versionの回帰が疑われる場合だけ、既知のversionを`--codex-version`と`--claude-version`で明示してimageを再buildします。active `oauth-token`やquarantineを削除せず、legacy quarantineの復元は自動化しません。Phase 2はhostの`~/.claude`をmountせず、migration sourceをread-onlyに扱い、旧claude-containerを変更しません（停止・rename・remove・rebuildもしません）。そのため、旧containerへ戻るときにPhase 2が旧containerのstateやGit repositoryを復元・変更することはありません。
