# Phase 1 Codex最小コンテナ設計

- 日付: 2026-08-22
- 状態: 承認済み
- 対象: `agent-container` Phase 1の最初のend-to-end slice
- 上位設計: `docs/superpowers/specs/2026-08-22-agent-container-design.md`

## 1. 目的

`agent-container`自身を最初の検証projectとして、GitHub上のoriginから分離workspaceを作り、rootless Podman内でCodexを起動する。CodexとGitHubの認証、project別の履歴・設定・cache、対象projectのhandoverだけを明確なmount境界で渡し、ホストの実設定や既存開発workspaceをコンテナへ見せない。

このsliceでは、Codexが対話実行でき、workspaceを編集し、GitHubへ作業ブランチをpushしてPRを扱えるための基盤を作る。family GitHub MCP、Claude Code adapter、network brokerは後続sliceへ分ける。

## 2. 採用方式

Python標準ライブラリで実装するホストランチャーと、固定したContainerfileを組み合わせる。

- Pythonランチャーはproject ID、repository、path、権限、既存workspaceを検証してからPodmanを呼ぶ。
- ContainerfileはCodex、GitHub CLI、Git、Pythonと最小限の実行依存を提供する。
- rootless Podmanの動的なmountと対話TTYはランチャーが構築する。
- Podman Composeは、projectごとにmountと状態を切り替える今回の用途では採用しない。
- shell scriptだけでの実装は、path検証とテスト可能性が弱くなるため採用しない。

ランチャーの利用者向けcommand名は`agentctl`とする。Python moduleとしても実行できる構造にし、package install前の開発中は`python3 -m agent_container.agentctl`で同じ処理を呼べるようにする。

## 3. 対象command

### `agentctl build`

repositoryのContainerfileから、local image `localhost/agent-container:dev`をrootless Podmanでbuildする。build contextはこのrepositoryだけとし、ホストのcredentialや状態directoryを含めない。

### `agentctl auth codex`

認証専用の一時containerを対話起動し、`codex login --device-auth`を実行する。`CODEX_HOME`は共有認証専用directoryを指し、file credential storeを明示する。成功後は`codex login status`で認証方式だけを確認し、credential値は出力しない。

### `agentctl project add OWNER/REPOSITORY`

repository名から既定project IDを作り、専用workspaceへcloneする。既定IDはrepository部分と同じ値とし、必要な場合だけ明示的な`--project ID`を許す。cloneは同じcontainer imageの一時container内で行い、専用GitHub認証だけをmountする。

workspaceがすでに存在する場合は上書きしない。安全なGit repositoryで、originが要求されたHTTPS URLと一致する場合だけ既存workspaceとして受け入れる。自動reset、clean、branch削除、force操作は行わない。

### `agentctl run PROJECT`

projectのworkspace、Codex状態、cache、共有認証、対象handoverだけをmountし、対話TTYでCodexを起動する。working directoryは`/workspace`とする。container本体は終了時に削除し、明示した永続directoryだけを残す。

### `agentctl doctor PROJECT`

Podmanのversionとrootless状態、image、必要directory、所有者とmode、認証ファイルの有無、workspaceとorigin、handover mount元を検査する。表示するのは有無、path、認証方式、検査結果だけとし、credential本文、環境変数値、設定ファイル本文は表示しない。

## 4. ホスト側directory構造

既定の状態rootは`${XDG_DATA_HOME:-~/.local/share}/agent-container`とする。環境変数`AGENT_CONTAINER_HOME`が設定された場合は、その絶対pathを明示的な状態rootとして使う。

```text
agent-container/
├─ gh/                              GitHub CLI専用認証
├─ shared-auth/
│  └─ codex/
│     └─ auth.json                  Codex共有ログイン情報
├─ projects/
│  └─ agent-container/
│     ├─ codex-home/                project別session、config、trust状態
│     └─ cache/                     project別cache
└─ workspaces/
   └─ agent-container/              originからcloneした作業場所
```

状態root、`gh`、`shared-auth/codex`、各project directoryは所有者だけがアクセスできるmode `0700`を要求する。credential fileはmode `0600`を要求する。ランチャーが新規作成する場合は最初からこのmodeを使い、既存pathのmodeが広い場合は自動変更せず拒否して修正方法を表示する。

ホストの実`~/.codex`、`~/.claude`、既存の開発workspaceは読み書きともmountしない。

## 5. Codex認証とproject分離

CodexはChatGPT subscription loginを使用し、認証情報の保存先をfileへ固定する。

```toml
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
```

OpenAI公式仕様では、file方式のcredentialは`CODEX_HOME/auth.json`に保存され、ChatGPT login tokenは利用中に自動更新される。そのため、`agentctl auth codex`では共有認証directoryを一時的な`CODEX_HOME`として使う。

通常実行時はproject別`CODEX_HOME`を`/home/agent/.codex`へmountし、共有`auth.json`だけをその中の`auth.json`へread-writeのfile bind mountとして重ねる。session、transcript、config、Skill、hook trust、cacheは共有せずproject別directoryへ残す。

実装前提として、Codexがtoken更新時にfile bind mountされた`auth.json`を正常更新できるか実containerで検証する。更新方式との互換性がない場合は、認証を各projectへ複製する実装へ進まず設計を再検討する。credentialの無断複製をfallbackにしない。

`auth.json`はパスワード相当として扱い、ログ、command line引数、Git、handover、test fixture、モデルへのpromptへ本文を出さない。

## 6. GitHub認証

GitHub CLIは専用`GH_CONFIG_DIR`を使用する。現在の検証環境では`/home/tsu/.local/share/agent-container/gh`にOAuth credentialが保存されているが、実装は状態rootから導出し、ユーザー名を固定しない。

通常のclone、fetch、push、PR操作では専用`gh` directoryをcontainerの`/home/agent/.config/gh`へread-only mountし、`GH_CONFIG_DIR`も同じpathへ設定する。Git HTTPS credential helperはcontainer内の`gh auth git-credential`を使用する。ホストrepositoryの暫定credential helper設定をcontainer設計の前提にしない。

ログイン更新やscope変更は通常containerから行わず、明示的な認証保守commandを将来追加する。初期sliceではホスト側で準備済みの専用GitHub認証を使用する。

GitHub credentialはcontainer内のCodexと、その子processから読める。完全なsecret brokerとは主張せず、自分の開発repository用credential、mount範囲、repository運用ルールで影響範囲を限定する。

## 7. Container境界

通常containerには次を適用する。

- rootless Podmanで実行する。
- `--rm`で終了時にcontainer本体を削除する。
- host userと整合するuser namespaceを使い、workspaceの生成物をhost user所有にする。
- privileged modeを使わない。
- capabilityをすべてdropする。
- `no-new-privileges`を設定する。
- root filesystemをread-onlyにする。
- `/tmp`と実行時に必要な一時領域はtmpfsにする。
- 永続書き込みをworkspace、project別Codex state、project別cache、対象handoverに限定する。
- `/workspace`以外のホスト開発repositoryをmountしない。
- SSH agent socket、Docker/Podman socket、ホストのcredential storeをmountしない。

mount対応は次のとおり。

| Host | Container | Mode |
|---|---|---|
| `workspaces/<project>` | `/workspace` | read-write |
| `projects/<project>/codex-home` | `/home/agent/.codex` | read-write |
| `shared-auth/codex/auth.json` | `/home/agent/.codex/auth.json` | read-write |
| `projects/<project>/cache` | `/home/agent/.cache` | read-write |
| `gh` | `/home/agent/.config/gh` | read-only |
| `handovers/<project>` | `/handovers/<project>` | read-write |

handover rootとしてcontainerに見せる`/handovers`には、選択projectのsubdirectoryだけが存在する構成にする。Obsidian vaultの親directoryや他projectのhandoverはmountしない。`AGENT_HANDOVER_ROOT=/handovers`と`AGENT_PROJECT_ID=<project>`をランチャーが設定する。

## 8. Profile配布

repository内`profiles/codex/`を管理された配布元とする。image build時にread-onlyのbase profileとして格納し、projectを初期化するときにproject別`CODEX_HOME`へ必要なconfig、Skill、hook定義をcopyする。

配布元はcontainer内で変更不可とし、runtime側のcopyはCodexがhook trustや対話設定を保存できるよう書き込み可能にする。base profile versionをproject stateへ記録し、後続実装で明示的なupdate commandを追加できる構造にする。初期sliceでは既存runtime設定を暗黙に上書きしない。

## 9. Network方針

初期sliceはOpenAIとGitHubへの接続、package取得を可能にするため、rootless Podmanの通常の外向きnetworkを使用する。Podman単体でのdomain allowlistを初期受け入れ条件にはしない。

この段階では「outbound networkを必要宛先だけへ制限済み」と主張しない。family MCP、credential broker、egress proxyまたはnetwork policyは後続強化として扱う。containerへhost network modeは与えない。

## 10. 入力検証と失敗時動作

project IDは既存handover実装と同じrepository-style slugだけを許す。GitHub repositoryは`OWNER/REPOSITORY`の2要素だけを許し、control character、空要素、`.`、`..`を拒否する。

すべての状態pathは解決後に状態rootまたは明示handover rootの内側であることを確認する。mount元そのもの、または安全性判断に使うproject directoryがsymlinkの場合は拒否する。

次の場合はPodmanを起動する前に失敗する。

- Podmanが存在しない、またはrootlessでない。
- imageが必要なcommandでimageが存在しない。
- 認証fileがない、所有者が異なる、modeが広すぎる。
- workspaceがGit repositoryでない。
- workspaceのoriginが要求repositoryと一致しない。
- 既存workspaceを上書きまたは削除する必要がある。
- handover mount元が選択projectと対応しない。

診断messageは問題のpathと直し方を示すが、secret本文を含めない。subprocess失敗時はexit codeを保持し、成功と誤表示しない。cleanupのためにworkspaceや認証状態を削除しない。

## 11. Test戦略

### 単体test

Podmanを起動せず、次をPython `unittest`で検証する。

- project IDと`OWNER/REPOSITORY`のaccept/reject。
- 状態root配下のpath導出。
- symlinkとroot外pathの拒否。
- permission check。
- 各commandが生成するPodman引数とmount mode。
- secret値が引数、例外、診断出力へ入らないこと。

### fake executable integration test

一時directoryのfake `podman`と`git`を使い、実行されたargvと環境変数名だけを記録する。credential fixtureは実token形式を使わず、本文を読み取る実装がないことを確認する。

### 実host smoke test

明示承認のもとでrootless Podmanを使い、次を順に確認する。

1. image buildが成功する。
2. `agentctl doctor agent-container`がrootless、権限、認証、workspaceをpassと判定する。
3. 専用workspaceへprivate repositoryをcloneできる。
4. container内`gh auth status`がアカウント名と認証方式だけを確認できる。
5. `codex login status`がChatGPT loginを確認できる。
6. Codex TUIを起動し、`/hooks`でSessionStart hookをtrustできる。
7. statuslineにmodel、context、利用limit、token、branch、projectが利用可能な範囲で表示される。
8. 最新handoverのpathだけが通知され、本文は自動注入されない。
9. container再起動後に同じprojectのsessionをresumeできる。
10. test用branchをpushし、PR作成まで到達できる。

実GitHubに作るtest branchとPRは、名前と目的を明示してから作成する。`main`への直接push、merge、force-push、release、repository削除はsmoke testに含めない。

## 12. 初期受け入れ条件

- `agentctl build`、`auth codex`、`project add`、`run`、`doctor`のinterfaceが定義どおり動く。
- rootless Podman以外では通常起動を拒否する。
- ホスト実`~/.codex`、`~/.claude`、既存workspace、Obsidian vault全体をmountしない。
- project AのCodex session、cache、設定がproject Bの通常mountから見えない。
- Codex認証は共有できるが、credential本文を診断・ログ・Gitへ出さない。
- GitHub認証は専用directoryだけから読み、container内でclone、branch push、PR作成ができる。
- handoverは選択projectだけ見え、startup hookがpathだけを通知する。
- statuslineとsession resumeを認証済みTUIで確認する。
- network allowlist未実装であることをdocumentし、制限済みと誤認させない。

## 13. 初期スコープ外

- family repository用GitHub MCP/broker。
- Claude Code adapterと既存`claude-container`からの移行。
- domain単位のoutbound network allowlist。
- credentialをcontainerへ渡さない外部broker。
- resource使用量の常時statusline表示。
- 自動merge、`main`直接push、force-push、release、repository設定変更。
- 複数host、Docker、macOS、Windowsの正式対応。

## 14. 参照仕様

- OpenAI公式「Authentication」: https://learn.chatgpt.com/docs/auth
- 上位設計: `docs/superpowers/specs/2026-08-22-agent-container-design.md`
- Codex運用プロトタイプ: `docs/codex-operations.md`
