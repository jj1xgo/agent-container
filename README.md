# agent-container

AI coding agentsをホスト環境から分離して動かす、Linux・rootless Podman向けの開発環境です。CodexとClaude Codeを、projectごとに分けたworkspace・設定・cache・handoverとともに実行します。

Current release: `v0.1.0`

Development version: `0.2.0-dev.0`

現在はPhase 3のGitHub App brokerを明示opt-inで利用できます。`0.x`の間はCLI、state配置、security boundaryが互換性なく変更される可能性があります。

## 最短で使う

必要なsoftwareを準備したLinux hostで、repositoryをcloneしてsetup scriptを実行します。引数は、agentに作業させるGitHub repositoryです。

```bash
git clone https://github.com/jj1xgo/agent-container.git
cd agent-container
bin/setup.sh OWNER/REPOSITORY
```

scriptは専用GitHub認証、image build、Codex認証、project登録、診断を順番に案内します。認証済みの項目とbuild済みimageは再利用するため、途中で止まっても同じcommandを再実行できます。既定ではstateとhandoverを`~/.local/share/agent-container`以下へ保存します。

setup完了後は、次の一行でCodexを起動します。

```bash
bin/agentctl run REPOSITORY
```

別名で登録する場合は、第2引数にproject名を指定します。

```bash
bin/setup.sh OWNER/REPOSITORY PROJECT_NAME
bin/agentctl run PROJECT_NAME
```

## 必要なもの

- Linux
- rootless modeで動くPodman 5.8以降
- Python 3.11以降
- Git
- GitHub CLI (`gh`)
- Codexを使う場合はOpenAIアカウント、Claude Codeを使う場合はClaude subscription

Podmanがrootlessで動いていることを確認します。

```bash
podman info --format '{{.Host.Security.Rootless}}'
```

`true`にならない環境ではagent-containerを実行できません。

## どこからでも`agentctl`を使えるようにする

aliasではなく、agent-containerの`bin` directoryを`PATH`へ追加する方法を推奨します。shellやscriptから通常のcommandとして`agentctl`を実行でき、README内の`bin/agentctl`は`agentctl`と読み替えられます。

ホスト上でcloneしたagent-container directoryへ移動し、次を実行します。

```bash
cd /path/to/agent-container
printf '\nexport PATH="%s/bin:$PATH"\n' "$PWD" >> "$HOME/.bashrc"
source "$HOME/.bashrc"
```

`/path/to/agent-container`は実際のclone先へ置き換えてください。container内の一時的な`/workspace`ではなく、ホスト上の永続的な絶対pathを使用します。

設定後は次のcommandで確認できます。

```bash
command -v agentctl
agentctl --version
```

状態rootも常に同じ場所を使う場合は、次の設定も`~/.bashrc`へ追加できます。

```bash
printf 'export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"\n' >> "$HOME/.bashrc"
source "$HOME/.bashrc"
```

すでに同じ設定が`~/.bashrc`にある場合は重複して追記せず、既存行を利用してください。

## 手動でセットアップする

保存先や各工程を個別に管理したい場合は、以下を順番に実行します。agent-container repositoryをcloneし、そのdirectoryで実行してください。

```bash
git clone https://github.com/jj1xgo/agent-container.git
cd agent-container

export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"
mkdir -p "$AGENT_CONTAINER_HOME/gh"
chmod 700 "$AGENT_CONTAINER_HOME" "$AGENT_CONTAINER_HOME/gh"

GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh" gh auth login --git-protocol https

bin/agentctl build
bin/agentctl auth codex
```

agent-containerは通常の`~/.config/gh`や`~/.codex`をcontainerへ渡しません。上の`gh auth login`はagent-container専用のGitHub認証を作り、`auth codex`は認証専用containerでCodexのdevice loginを開始します。tokenや認証fileの内容をcommand line、ログ、handoverへ貼り付けないでください。

次に、handoverの保存先と対象project用の空directoryを作ります。`HANDOVER_ROOT`は自分が管理する絶対pathへ置き換えてください。

```bash
export HANDOVER_ROOT="$HOME/handovers"
mkdir -p "$HANDOVER_ROOT/REPOSITORY"

bin/agentctl project add OWNER/REPOSITORY \
  --handover-root "$HANDOVER_ROOT"
```

既定のproject名はrepository名です。たとえば`jj1xgo/agent-container`なら`agent-container`になります。同名projectを区別したい場合は`--project PROJECT_NAME`を追加します。この場合、handover directoryも`$HANDOVER_ROOT/PROJECT_NAME`として先に作ってください。

新規projectのCodex stateには、`gh pr view/list/checks/status`、`gh issue view/list`、`gh run view/list`、`gh repo view`だけを読み取り用の初期approval rulesとして配置します。既存projectのrulesは暗黙に追加・上書きしません。

handover作成には保存先を環境から固定する専用`agent-handover create`を使い、このcommandだけを初期approval rulesで事前許可します。これによりhandoverごとの承認は不要です。既存projectではimage更新後、次のcommandでcustom rulesを残したまま専用ruleを追加し、managed handover skillを更新します。

```bash
bin/agentctl project update-profile PROJECT
```

診断がPASSしたらCodexを起動できます。

```bash
bin/agentctl doctor REPOSITORY
bin/agentctl run REPOSITORY
```

`doctor`の既定agentと`run`の既定agentはCodexです。終了後もworkspace、agent設定、session、cache、handoverはproject別のstateに残ります。

## Claude Codeを使う

同じimageとprojectをClaude Codeでも利用できます。認証操作は、画面収録や入力記録のないprivate terminalで本人が実行してください。

```bash
bin/agentctl auth claude
bin/agentctl doctor REPOSITORY --agent claude
bin/agentctl run REPOSITORY --agent claude
```

`auth claude`では`claude setup-token`が表示したtokenを、その後に現れる非表示promptへ貼り付けます。tokenをchat、shell history、環境変数、screenshotへ保存しないでください。既存のClaude設定を移行する場合は、[Claude Code運用ガイド](docs/phase2-claude-code.md#初回claude-run前のmigration)にあるdry-run-firstの手順を使います。

CodexとClaudeをまとめて診断することもできます。

```bash
bin/agentctl doctor REPOSITORY --agent all
```

## 2回目以降の使い方

通常はagent-container repositoryへ移動し、初回と同じstate rootを指定してから診断・起動します。

```bash
cd /path/to/agent-container
export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"

bin/agentctl doctor PROJECT
bin/agentctl run PROJECT
```

Claude Codeなら両方に`--agent claude`を付けます。Codexでは対話画面の`/resume`から同じprojectの過去sessionを再開できます。

別のGitHub repositoryを使うときは、handover directoryを作ってから追加します。

```bash
mkdir -p "$HANDOVER_ROOT/ANOTHER_REPOSITORY"
bin/agentctl project add OWNER/ANOTHER_REPOSITORY \
  --handover-root "$HANDOVER_ROOT"
```

projectはagent-container専用state内へcloneされます。ホスト上の既存開発workspaceをmountしたり上書きしたりしません。

## Superpowers

新規projectには[Superpowers](https://github.com/obra/superpowers)をCodexとClaude Codeの両方へ自動導入します。Codexは遅延する可能性がある複製marketplaceではなく`obra/superpowers`本家の`main`を直接marketplace sourceとして登録し、install時点のsnapshotをproject別stateへ保存します。Claude Codeは公式plugin marketplaceの`superpowers@claude-plugins-official`を利用します。通常の`run`は保存済みsnapshotを使い、起動のたびに外部sourceを更新しません。

本家と公式pluginの最新版へ明示的に更新するには、対象projectまたは登録済みの全projectを指定します。

```bash
bin/agentctl superpowers update PROJECT
bin/agentctl superpowers update --all-projects
```

更新commandは外向きnetworkを使用します。更新後は対象projectで`doctor`を実行し、実際の開発作業を始める前にSuperpowersのversionとskill一覧を対話画面で確認してください。

## GitHub App brokerを使う

Phase 3のbroker modeでは、GitHub App private keyとinstallation tokenをhost側だけに置き、containerへはproject別Unix socketと一時的なcapabilityだけを渡します。exact repositoryのclone/fetch、作業branch push、`agent-github pr create/view/checks`だけを提供し、merge、release、generic APIは提供しません。

GitHub Appのselected repository installation、最小permission、全branch force-push禁止ruleset、private stateを準備してから、project登録・doctor・runに`--github-broker`を明示します。

```bash
bin/agentctl project add OWNER/REPOSITORY \
  --handover-root "$HANDOVER_ROOT" \
  --github-broker \
  --protected-branch main \
  --confirm-force-push-ruleset

bin/agentctl doctor REPOSITORY --github-broker
bin/agentctl run REPOSITORY --github-broker
```

broker failureから専用`gh` credentialへ自動fallbackしません。設定と実host検証の全手順は[Phase 3 GitHub App broker運用ガイド](docs/phase3-github-broker.md)を参照してください。

## Imageの更新

次のcommandはagent用Node.js、Codex、Claude Codeの現在のlatestを解決してimageを再buildします。

```bash
bin/agentctl build
```

runtime中のself-updateは無効です。version変更は再build時だけ発生します。問題調査やrollbackでversionを固定する方法は[Claude Code運用ガイド](docs/phase2-claude-code.md#image-build更新rollback)を参照してください。

project固有のDebian packageやNode.js versionは、対象repositoryの`.agent-container.d/packages.txt`と`.agent-container.d/node-version.txt`で宣言できます。設定が変わると次回`run`時にproject用imageが自動buildされます。

## 主なcommand

| Command | 用途 |
| --- | --- |
| `bin/agentctl --version` | agent-containerのversionを表示 |
| `bin/agentctl build` | 共通runtime imageをbuild・更新 |
| `bin/agentctl auth codex` | Codex専用認証を作成・更新 |
| `bin/agentctl auth claude` | Claude専用認証を作成・更新 |
| `bin/agentctl project add OWNER/REPOSITORY --handover-root PATH` | projectを専用workspaceへ登録 |
| `bin/agentctl project update-profile PROJECT` | 既存projectのmanaged handover skillと専用approval ruleを更新 |
| `bin/agentctl superpowers update PROJECT` | 対象projectのSuperpowersを明示的に最新版へ更新 |
| `bin/agentctl superpowers update --all-projects` | 登録済み全projectのSuperpowersを最新版へ更新 |
| `bin/agentctl doctor PROJECT [--agent codex\|claude\|all]` | 起動前の状態をread-onlyで診断 |
| `bin/agentctl run PROJECT [--agent codex\|claude]` | agentを起動 |
| `bin/agentctl stats PROJECT` | 実行中agent containerのsecret-free resource snapshotを表示 |
| `bin/agentctl project add ... --github-broker --confirm-force-push-ruleset` | GitHub App broker modeでprojectを登録 |
| `bin/agentctl doctor PROJECT --github-broker` | local broker stateとproject policyを診断 |
| `bin/agentctl run PROJECT --github-broker` | credential-free Git/PR broker付きでagentを起動 |

## 困ったとき

- `doctor`が`FAIL`を出したら、その項目を解消してから`run`してください。`WARN network-policy`は、現在の外向き通信がdomain allowlistされていないことを示す既知の警告です。
- `project add`がhandover pathを拒否する場合は、handover rootが絶対pathであり、`HANDOVER_ROOT/PROJECT`が実在する通常directoryか確認してください。symlinkは利用できません。
- GitHub認証で失敗する場合は、`GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh" gh auth status`で専用認証の状態を確認してください。token本文は表示・記録しないでください。
- Codex認証は`bin/agentctl auth codex`、Claude認証は`bin/agentctl auth claude`で更新します。認証fileを直接編集・表示しないでください。
- 詳しい運用とsecurity boundaryは[Codex運用ガイド](docs/phase1-codex-container.md)と[Claude Code運用ガイド](docs/phase2-claude-code.md)を参照してください。

## Security boundary

runtimeはrootless Podman、read-only root filesystem、capability削除、`no-new-privileges`、限定したmountを使用します。一方、外向きnetworkはdomain allowlistされていません。containerは被害範囲を狭める境界であり、agentへ渡したcredentialの完全な秘密保持を保証するものではありません。

`main`への直接push、force-push、merge、release、repository削除は標準操作に含みません。変更は作業branchとPRでreviewしてください。

## 詳細資料

- [Phase 1 Codex operator guide](docs/phase1-codex-container.md)
- [Phase 1実host smoke test](docs/phase1-smoke-test.md)
- [Phase 2 Claude Code operator guide](docs/phase2-claude-code.md)
- [Phase 2実host smoke test](docs/phase2-smoke-test.md)
- [Phase 3 GitHub App broker operator guide](docs/phase3-github-broker.md)
- [Phase 3実host smoke test](docs/phase3-github-broker-smoke-test.md)
- [Phase 3 resource監視・cross-agent review](docs/phase3-resource-review.md)
- [設計文書](docs/superpowers/specs/2026-08-22-agent-container-design.md)
- [Changelog](CHANGELOG.md)

## License

GNU General Public License v3.0。詳細は[LICENSE](LICENSE)を参照してください。
