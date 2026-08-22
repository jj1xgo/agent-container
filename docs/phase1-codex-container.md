# Phase 1 Codex container 運用ガイド

このガイドは、rootless PodmanでCodexを専用workspaceに隔離して使うPhase 1の初回手順です。設計上の境界と制約は[Phase 1設計](superpowers/specs/2026-08-22-phase-1-codex-container-design.md)を参照してください。

## 開始前

- Linux上のrootless Podmanを使用します。host network、Podman socket、既存開発workspace、他projectのhandoverはcontainerへ渡しません。
- ホストの`~/.codex をmountしません`。Codex認証は状態rootの`shared-auth/codex/auth.json`だけを用途限定で扱います。
- GitHub CLIの認証（`gh`）は事前に専用状態directoryへ準備します。GitHub認証の更新やscope変更は通常runの外で行います。
- credential本文、device code、token、`auth.json`の内容を画面、ログ、handover、issue、PRへ表示しません。
- Phase 1では`外向き通信はドメイン制限されていません`。OpenAI、GitHub、package取得を使える通常のrootless Podman networkであり、domain allowlist済みとは主張しません。

状態rootは既定で`${XDG_DATA_HOME:-~/.local/share}/agent-container`です。初回は、用途を明示するため次の例のように設定します。

```bash
export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"
bin/agentctl build
bin/agentctl auth codex
bin/agentctl project add jj1xgo/agent-container \
  --handover-root "$HOME/obsidian-vault/handovers"
bin/agentctl doctor agent-container
bin/agentctl run agent-container
```

`$HOME/obsidian-vault/handovers`はhandover rootの例です。実際に自分が管理する絶対pathへ置き換え、対象projectのhandoverだけがcontainerから見えることを確認します。tokenをshell変数、command line、記録に埋め込まないでください。

## 各commandの意味

`bin/agentctl build`はrepositoryのContainerfileから`localhost/agent-container:dev`をbuildします。build contextへcredentialや状態directoryを入れません。managed imageにはCodexのLinux sandbox用としてDebianの`bubblewrap`を含めます。

`bin/agentctl auth codex`は認証専用containerでdevice codeによるCodexログインを開始します。利用者がブラウザで完了します。成功確認は`codex login status`の認証方式だけで行い、`auth.json`を表示・読み取りしません。認証fileは`0600`、状態directoryは`0700`でなければならず、modeが広い既存pathは自動修正せず診断に従って修正します。

`bin/agentctl project add OWNER/REPOSITORY --handover-root ABSOLUTE_PATH`は専用状態rootのworkspaceへcloneします。既存workspaceを上書きしません。すでにworkspaceがある場合は、要求repositoryのHTTPS originと一致するときだけ既存workspaceとして受け入れます。

`bin/agentctl doctor PROJECT`はrootless状態、image、private mode、認証fileの存在、workspace origin、handover mount元を検査します。出力にはpath、存在、認証方式、検査結果だけを出し、credential本文や環境変数値は出しません。

`bin/agentctl run PROJECT`は対象projectのworkspace、project別Codex stateとcache、共有`auth.json`、専用`gh`認証、対象handoverだけをmountしてCodexを起動します。終了時にcontainer本体は削除され、明示した永続directoryだけが残ります。

## 日常の運用

開始前に`bin/agentctl doctor agent-container`を実行し、FAILを解消してから`bin/agentctl run agent-container`を実行します。初回またはhook定義変更後はCodex内の`/hooks`で内容とtrust状態を確認します。`/statusline`と`/status`で表示項目を確認し、必要に応じて`/resume`で同じprojectのsessionを再開します。

handover hookは最新handoverの**pathだけ**を通知します。本文の自動表示やcredentialの記録はしません。

GitHubへの変更は通常のreview workflowに従います。`main`へ直接pushしないでください。branchのpushとPR作成は、作業内容・branch名・PR目的を明示し、別途利用者の承認を得てから実行します。merge、force-push、release、repository削除はPhase 1の操作に含めません。

## 障害時

- `doctor`がrootless以外、image未build、mode不正、認証file不足、workspace origin不一致を示す場合は、表示された対象pathだけを確認して修正します。credential本文を調査目的で出力しません。
- Codex認証を確認するときは`codex login status`のみを使います。`auth.json`に対して`cat`、`jq`、`sed`など本文を出すcommandは実行しません。
- 認証fileの更新でwriteまたはrename errorが出た場合は作業を停止し、認証をprojectごとに複製しません。設計を見直します。
- 認証済みTUIを含む実host確認は[smoke test checklist](phase1-smoke-test.md)に従い、未実施の項目をPASSと扱いません。
