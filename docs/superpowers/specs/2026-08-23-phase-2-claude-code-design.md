# Phase 2: Claude Code基本対応 設計

**作成日:** 2026-08-23  
**対象:** `agent-container`  
**状態:** ユーザー承認済み

## 1. 目的

Phase 1で確立したrootless Podman、project別workspace、private state、GitHub認証、handover境界を維持したまま、Claude Codeを第2の対話agentとして実行できるようにする。

Phase 2の完了時には、利用者が次を実行できることを目標とする。

- agent-container専用領域へClaude Codeで新規ログインする。
- 同じproject workspaceをCodexまたはClaude Codeから選択して開く。
- Claude Codeの設定、履歴、pluginsをproject間で分離し、認証ファイルだけを共有する。
- 旧`claude-container`を変更せず、明示した設定だけをdry-run後にcopy移行する。
- Claude Codeで基本的な編集、test、commitを実行する。
- 既存のCodex運用を壊さない。

## 2. 非目標

次はPhase 2の対象外とする。

- ホストの実`~/.claude`または旧`claude-container`の直接mount。
- 旧Claude認証、session履歴、transcript、cache、handoverの移行。
- CodexとClaude Codeの同時実行制御。
- Claude Code向けhandover自動発見hook。
- domain allowlistによるegress制限。Phase 1と同様に既知のWARNとして残す。
- API key、Bedrock、Vertex AI、Foundryを既定認証にすること。
- agentごとの派生imageや複数Containerfileへの分割。

## 3. 採用する構成

CodexとClaude Codeを同じimageへinstallし、起動時の`--agent`で選択する。workspace、GitHub認証、handover mount、Podmanの保護設定は共通化し、agent固有の状態とcommand specだけを分ける。

別image方式はbuild、doctor、共通境界の実装が重複するため採用しない。共通baseと派生imageの組み合わせはplatformが増えるまで導入しない。

## 4. 永続状態

状態root配下を次のようにする。

```text
<state-root>/
├── shared-auth/
│   ├── codex/
│   │   └── auth.json
│   └── claude/
│       └── .credentials.json
├── projects/<project>/
│   ├── codex-home/
│   ├── claude-config/
│   ├── cache/
│   └── project.json
└── workspaces/<project>/
```

`shared-auth/claude`、`projects/<project>/claude-config`をmode `0700`、`.credentials.json`をmode `0600`かつ実行user所有とする。symlinkおよび想定root外へ解決されるpathを拒否する。

Claude Codeの認証だけを全projectで共有する。設定、session履歴、plugins、memoryなどは`CLAUDE_CONFIG_DIR`が指すproject別`claude-config`へ保存する。cacheは既存のproject別cache mountを再利用し、project間では共有しない。

`project add`では`claude-config`をまだ作らない。新旧どちらのprojectも、最初の`run --agent claude`で空directoryを作るか、最初の`migrate claude --apply`でstaged directoryを配置する。これにより、未使用projectへ不要な状態を作らず、移行時はdirectory単位のatomic renameを使える。`doctor`はread-only診断のためdirectoryを作成しない。

## 5. Image buildとversion管理

単一の`Containerfile`へClaude Codeを追加する。Nodeベースimageを維持し、CodexとClaude Codeをnpmからglobal installする。

通常の`bin/agentctl build`は毎回両packageの`latest`を対象にする。CLI install layerへbuildごとに異なるcachebusterを渡し、Dockerfileが変わらない場合にも古いnpm layerを再利用しない。実行中の自己更新は無効化し、versionが変わる時点をimage buildへ限定する。

障害調査とrollbackのため、次の明示version overrideを提供する。

```bash
bin/agentctl build \
  --codex-version VERSION \
  --claude-version VERSION
```

version値は空文字、optionに見える値、空白、control characterを拒否する。build後に一時containerで`codex --version`と`claude --version`を実行し、両方がexit 0であることを必須とする。表示するのはCLI名とversionだけで、credentialや環境変数値を含めない。

## 6. CLI contract

既存互換のため、`run`と`doctor`のagent既定値は`codex`とする。

```bash
bin/agentctl build
bin/agentctl build --codex-version VERSION --claude-version VERSION

bin/agentctl auth codex
bin/agentctl auth claude

bin/agentctl run PROJECT
bin/agentctl run PROJECT --agent codex
bin/agentctl run PROJECT --agent claude

bin/agentctl doctor PROJECT
bin/agentctl doctor PROJECT --agent codex
bin/agentctl doctor PROJECT --agent claude
bin/agentctl doctor PROJECT --agent all

bin/agentctl migrate claude PROJECT --from ABSOLUTE_PATH
bin/agentctl migrate claude PROJECT --from ABSOLUTE_PATH --plugin IDENTIFIER --apply
```

未知のagent、version、project ID、plugin識別子をPodman起動やfile作成より前に拒否する。plugin識別子はpath separator、`.`、`..`、control characterを許可しない。

## 7. Claude認証

`agentctl auth claude`は次の順で動く。

1. configured state rootの実path、所有者、modeを検証する。
2. rootless Podmanとimageの存在を検証する。
3. `shared-auth/claude`だけを認証用containerへread-write mountする。
4. `CLAUDE_CONFIG_DIR`をそのmount先へ設定し、`claude auth login`を実行する。
5. `.credentials.json`が通常ファイル、非symlink、mode `0600`、実行user所有であることを検証する。
6. 同じ限定mountを使って`claude auth status`を実行し、exit 0を必須とする。

認証はClaude.ai/Consoleの対話選択に任せ、Phase 2ではlogin methodを強制しない。API keyやOAuth tokenをcommand line、log、test fixture、handoverへ出力しない。

## 8. Claude runtime adapter

`run_claude_spec()`をCodex adapterと分離して追加する。Podmanの共通prefixとGitHub credential helperは再利用する。

Claude実行containerへ渡すものは次に限定する。

| Host source | Container target | Mode |
|---|---|---|
| 選択projectのworkspace | `/workspace` | read-write |
| 選択projectの`claude-config` | `/home/agent/.claude` | read-write |
| 共有`.credentials.json` | `/home/agent/.claude/.credentials.json` | read-write file mount |
| 選択projectのcache | `/home/agent/.cache` | read-write |
| 専用GitHub config | `/home/agent/.config/gh` | read-only |
| 選択projectのhandover directory | `/handovers/<project>` | read-write |

`CLAUDE_CONFIG_DIR=/home/agent/.claude`と`AGENT_PROJECT_ID`、`AGENT_HANDOVER_ROOT=/handovers`を設定し、`claude`を起動する。Claude固有のpermission bypass optionは渡さない。

共通runtime保護は次を維持する。

- rootless Podman
- `--read-only`
- `--cap-drop=all`
- `--security-opt=no-new-privileges`
- host userと一致するkeep-id user namespace
- `/tmp`だけのtmpfs

認証file単独のnested bind mountがClaude Codeのrefresh書き込みと両立することを実hostで確認する。更新が失敗する場合、共有config directory全体のmountやcredential copyへ黙って緩和せず、Phase 2を未完了として設計を再検討する。

## 9. 限定copy移行

移行元を自動検出しない。利用者は旧projectのClaude config directoryを絶対pathで`--from`へ渡す。既定はdry-runであり、`--apply`がない限りdestinationを変更しない。

### 9.1 既定allowlist

次だけを候補にする。

```text
CLAUDE.md
settings.json
agents/
commands/
rules/
skills/
hooks/
```

次は常に除外する。

- `.credentials.json`、`.claude.json`、認証・token file
- `projects/`、`sessions/`、`transcripts/`、`handovers/`、`plans/`
- `state/`、cache、log、test result、scratchpad
- `.git/`を含むVCS metadata
- allowlist外のfileとdirectory

### 9.2 Plugins

pluginsは無指定時にすべて除外する。`--plugin IDENTIFIER`を繰り返し指定したものだけを候補にする。識別子は移行元の`installed_plugins.json`にある完全一致keyを使う。

移行処理はmanifestをJSONとして検証し、選択されたpluginのexact version実体と必要なmarketplace metadataだけを辿る。参照先がsource root外、欠落、symlink、未知schemaである場合はそのpluginだけを推測copyせず、移行全体を失敗させる。

### 9.3 File安全性と適用

- sourceは絶対pathの実directoryで、symlinkを含まず、正規化後も同一pathであること。
- 通常fileとdirectoryだけを扱い、symlink、socket、deviceなどを拒否する。
- 各候補pathがsource root内に留まることをcopy直前にも再検証する。
- destinationの`claude-config`自体が存在したら、空であっても何もcopyせず失敗する。移行は最初のClaude起動前に行う。
- destinationと同じparentに一時directoryを作り、全候補をcopy・検証してから`claude-config`へdirectory単位でrenameし、部分移行を残さない。
- directory modeは`0700`、通常fileは`0600`、sourceでuser実行bitがあるfileだけ`0700`とする。
- sourceの内容、環境変数値、credentialらしい値をdry-runやerrorへ表示しない。
- sourceはread-only入力として扱い、rename、chmod、削除を行わない。

`settings.json`はJSON objectであることを検証する。`apiKeyHelper`を拒否し、`env`内のkey名が大文字小文字を無視して`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL`、`API_KEY`、`AUTH`を含む場合はfile全体を拒否する。これは既知のcredential混入をfail closedにする検査であり、任意の自由記述から秘密を完全検出できる保証ではない。`CLAUDE.md`、hook、skillを含む移行候補は利用者がdry-run一覧とsourceをreviewしてから`--apply`する運用を必須とする。

## 10. Doctor

共通checkの後にagent別checkを追加する。

Claude checkは少なくとも次を含む。

- image内の`claude --version`
- `shared-auth/claude/.credentials.json`の存在、所有者、mode、非symlink
- project別`claude-config`の存在、所有者、mode、非symlink
- workspace origin
- handover project境界
- network policy WARN

`--agent all`は共通checkを一度だけ実行し、CodexとClaudeのagent別checkを順序固定で表示する。1つでもFAILがあればexit codeを非zeroにする。secretは存在とmodeだけを報告する。

## 11. Error handling

次はcontainer起動またはdestination変更より前に失敗させる。

- rootless Podmanでない、imageがない、CLI version probeが失敗する。
- state root、agent state、workspace、`.git`、handoverにsymlinkまたは不正modeがある。
- workspace originがproject metadataと一致しない。
- Claude認証がない、またはcredential fileがmode `0600`でない。
- 未対応agent、危険なversion値、危険なplugin識別子が指定される。
- migration sourceまたは候補が境界外へ解決される。
- migration destinationに衝突がある。

例外messageにはpath、check名、終了codeを含めてよいが、file本文、token、環境変数値は含めない。予期しないfilesystem `OSError`はcredential内容を出さない一般化messageへ変換する。

## 12. Test strategy

実装はtest-driven developmentで進める。

### 12.1 Unit tests

- CLI parserの既定agentと全subcommand。
- Claude state path、mode、所有者、symlink拒否。
- versionとplugin識別子validation。
- migration allowlist、denylist、衝突、atomic適用。
- `settings.json`のcredential key拒否。
- plugin manifestのexact selectionと境界外参照拒否。

### 12.2 Command-spec tests

- auth containerがClaude共有認証directoryだけをmountする。
- runtimeが選択projectのmountだけを持つ。
- GitHub configがread-onlyである。
- host `~/.claude`、旧`claude-container`、他project stateをargvへ含めない。
- Podman保護flagとClaudeの非bypass起動を検証する。
- buildに両versionとcachebusterが渡る。

### 12.3 Orchestration tests

- auth、run、doctor、migrateの検証順序とfail-safe動作。
- Phase 1 projectのClaude state初期化。
- `doctor --agent all`の共通check非重複とexit code。
- credentialやfixture markerをstdout/stderrへ出さない。
- 既存Codex testを変更後もすべて通す。

### 12.4 Image and host smoke tests

- image内の`codex --version`と`claude --version`。
- rebuildごとにlatest取得layerが再実行されること。
- Claude対話loginと`claude auth status`。
- container再起動後の認証継続とcredential refresh。
- Claudeによるtest用branchでの編集、test、commit。
- hostの実`~/.codex`、`~/.claude`、既存workspace、他project state、旧`claude-container`がmountされないこと。
- 旧`claude-container`のGit statusと対象stateが変更されていないこと。

実host smoke testの外部操作は専用test branchに限定する。mainへの直接push、force-push、merge、release、削除を標準手順へ含めない。

## 13. 実装単位

実装計画では少なくとも次の順で分割する。

1. CLI contractとstate layout。
2. build時のClaude install、latest cachebuster、version probe。
3. Claude auth adapter。
4. Claude runtime adapterとdoctor。
5. 限定copy migration。
6. 運用文書と自動test全体。
7. rootless Podman実host smoke test。

各単位は独立したRED、最小実装、GREEN、回帰testの順で進める。実host検証前に自動test全件を成功させる。

## 14. 根拠となる外部仕様

2026-08-23時点のClaude Code公式資料では、Linuxのcredentialは既定で`~/.claude/.credentials.json`へ保存され、`CLAUDE_CONFIG_DIR`を設定した場合はその配下へ移る。また、同directory配下に設定、session履歴、pluginsを分離できる。

- [Authentication](https://code.claude.com/docs/en/authentication)
- [Environment variables](https://code.claude.com/docs/en/env-vars)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Development containers](https://code.claude.com/docs/en/devcontainer)
- [Advanced setup](https://code.claude.com/docs/en/installation)

公式資料は、再現可能なcontainer buildで特定versionを使う場合にnpmのversion指定と自動更新無効化を案内している。本設計は通常buildでは`latest`を明示的に再取得し、必要時だけversion overrideを使うことで、利用者が求めるbuild時更新とrollback可能性を両立する。
