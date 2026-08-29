# agent-container 初期設計

- 日付: 2026-08-22
- 状態: 承認済み
- 対象: 初期リリース（個人利用を優先し、公開可能な品質を目指す）

## 1. 目的

`agent-container` は、AIコーディングエージェントをホスト環境から分離して動かすための、Linux・rootless Podman向け開発環境である。

Codexを主対象としつつ、Claude Codeも早期から基本的な開発作業に使える構成にする。単なる使い捨てsandboxではなく、認証情報、エージェント設定、プロジェクト、GitHub権限の境界を明確にした、日常利用可能な個人用開発環境を目指す。

初期段階では汎用製品や他者へのサポート提供を前提にしない。ただし、ソースコードはGPLv3-onlyで公開できる構造と品質を維持する。

既存`claude-container`のforkや改名版にはしない。現在の設計担当Codexは既存実装を参照済みであるため、既存コードを転記する実装担当にはせず、仕様・レビュー・移行互換性の確認を中心に用いる。新実装は公開仕様と本設計を入力に、出所を説明できる形で作る。

## 2. 初期スコープ

### 対象

- Linuxホスト
- rootless Podman
- Codexの対話実行、編集、テスト、commit、作業ブランチへのpush、PR作成
- Claude Codeの基本的な対話実行と同等のGit作業
- 自分の開発リポジトリへの読み書き
- 選択中repositoryのIssue list/viewによるread-only access
- エージェント専用の認証・設定・履歴の永続化
- Codexのhandover運用、コンテキストと利用量の可視化

### 初期スコープ外

- macOS、Windows、Dockerの正式対応
- Kubernetesや複数ホストへの展開
- `main`への自動直接push
- 自動merge、force-push、release、リポジトリ削除・設定変更
- Claude Codeの高度なhooks、plugins、subagent運用の完全移植
- CPU、メモリ、ディスク使用量をCodexのTUIステータスラインへ統合すること
- 日英ドキュメントの完全な二重管理

## 3. 基本アーキテクチャ

構成は「共通基盤 + エージェント別adapter」とする。

```text
ホスト
├─ agent-containerのソース
├─ agent-container専用の永続領域
│  ├─ shared-auth/       Codex等の共有可能なログイン状態
│  ├─ projects/<id>/     プロジェクト別の履歴・cache・設定
│  └─ workspaces/<id>/   GitHubから取得した作業用clone
├─ 外部GitHub MCP/broker
│  └─ family用PATを保持（コンテナへ生PATを渡さない）
└─ handover保存先
   └─ 当面はObsidian vault、将来は専用private repository

コンテナ
├─ 共通の開発ツールとsandbox境界
├─ managed base config / skills / hooks（原則read-only）
├─ Codex adapter
├─ Claude Code adapter
└─ 選択したproject専用のworkspaceと状態
```

Codex用のstatusline、handover Skill、hooksの雛形は、初期段階では`agent-container`リポジトリ内のCodex adapterへ置く。個人の会話履歴、認証情報、handover本文はリポジトリへ含めない。別の`dotcodex-ops`リポジトリは、ホストと複数環境で設定を共用する必要が明確になった時点で分離を検討する。

### 信頼境界

- コンテナ内のエージェントと、そのエージェントが起動するprocessは、書き込み可能なmount内の情報を読めるものとして扱う
- credentialはログ、command line、prompt、handover、Git差分へ出さない
- secretを確認する診断は「存在・認証方式・許可対象」だけを表示し、生値を表示しない
- outbound networkは、package取得、GitHub、OpenAI/Anthropic、承認済みMCPなど、必要な宛先へ絞る
- mount、GitHub権限、MCP tool schema、network policyを重ね、単一の防御だけへ依存しない

## 4. workspace方針

ホストにある開発リポジトリをread-writeで直接マウントしない。GitHub上のoriginを基準に、agent-container専用領域へcloneして作業する。

標準フローは次のとおり。

1. originから専用workspaceへcloneまたはfetchする。
2. `main`を最新状態にする。
3. 作業ブランチを作る。
4. エージェントが変更、テスト、commitを行う。
5. 作業ブランチをoriginへpushする。
6. PRを作成する。
7. CI、テスト、別エージェントのレビュー結果を日本語で要約する。
8. ユーザーは実装コードそのものではなく、目的、動作、検証結果、残存リスクを確認する。

ホスト側にだけ存在する未commit・未pushの変更はコンテナから見えない。必要な変更は先にGitHubへ反映するか、将来設計する明示的なimport機能で扱う。

## 5. GitHub認証と権限分離

### 自分の開発リポジトリ

- 用途: clone、fetch、作業ブランチへのpush、PR作成
- 初期方式: `gh`を利用する
- 認証: リポジトリとセッションを限定してコンテナへ渡す方式から開始する
- 将来: 外部broker方式へ強化し、生のcredentialをコンテナへ渡さない構成を目指す

初期方式では開発用credentialがコンテナ内プロセスから絶対に見えないとは主張しない。対象リポジトリ、権限、有効期間を絞り、漏えい時の影響を限定する。

### familyリポジトリ

- 2026-08-29のscope変更前の初期案では、用途をコードread、Issue read/create/commentとしていた。この権限分離の歴史的な狙いは、family用credentialを開発repositoryのcredentialと混同しないことにある。
- 現行のshipped interfaceは、選択中repositoryのIssue list/viewだけを提供するread-only brokerである。family Issue create/commentは提供しない。
- コードwrite、push、merge、release、管理設定変更は提供しない。
- credentialはhost側brokerだけが保持し、生値をcontainerやmodel contextへ渡さない。

用途の違うPATを同じ環境変数名で切り替える設計にはしない。ツール境界そのものを分け、取り違えを防ぐ。

## 6. 永続化と分離

ホストの実`~/.codex`や`~/.claude`はマウントしない。agent-container専用の領域だけを永続化する。

`.codex`全体を全リポジトリで共有しない。

- 共有可能: agent-container専用のCodexログイン状態、管理された共通設定、承認済みSkill
- 分離する: session履歴、transcript、cache、project設定、workspace、project固有MCP
- read-only配布: base config、標準Skill、hook定義
- 書き込み可能: runtime stateと選択中projectのworkspace

Claude Codeも同じ原則を用いる。移行時に既存`~/.claude`を丸ごとマウントする方式は採用しない。

## 7. Codex handover運用

handoverは、Codex標準の会話継続機能と、外部の永続的な作業記録を使い分ける。

### レベル1: 同じ会話を継続

- `/rename`で会話に分かりやすい名前を付ける
- `/resume`または`codex resume`で再開する
- 日をまたいでも同じ作業を続けるだけなら、原則としてhandoverファイルを作らない

### レベル2: 同じ会話のcontextを整理

- contextが増えたら`/status`またはstatuslineで残量を確認する
- 必要に応じて`/compact`を使う
- compact後も重要な設計判断を再注入できるよう、`SessionStart`の`compact`イベントでproject要約を読み込める構造にする

### レベル3: 別セッション・別エージェントへ引き継ぐ

明示的なhandover Skillを実行し、次の項目を持つMarkdownを作る。

- 作業の目的
- 現在地
- 決定事項と理由
- 変更したファイル・commit・PR
- 実行した検証と結果
- 未解決事項、リスク、次の一手
- 認証情報を含まない参照先
- 元のCodex session IDまたは会話名

保存先は`handovers/<project>/YYYY-MM-DD_HHMM.md`とし、projectごとの最新handoverを機械的に特定できるようにする。

コンテナへObsidian vault全体をmountしない。当面は対象projectのhandover directoryだけを狭くread-writeで渡すか、ホスト側の保存helperを介する。専用private repositoryへ移行した後も、起動中projectに必要な範囲だけを見せる。

`SessionStart` hookはstartup/resume時に最新handoverの存在と要点を追加contextとして渡す。ただし、常に全文を注入せず、project、日時、概要、ファイルパスを基本とし、必要な場合だけCodexが本文を読む。

`SessionEnd` hookは索引更新や未保存の注意表示に限定する。終了時に transcriptから自動生成した文章を正本としない。`SessionEnd`は終了方法によって即時に動かない場合もあるため、handover完成の必須経路にはしない。突然終了した場合にも備え、作業中の事実はGit commit、PR、設計書にも残す。

## 8. Codex statuslineとリソース表示

Codex標準の`/statusline`と`tui.status_line`を利用し、初期推奨表示を次の順序とする。

1. model + reasoning
2. context使用状況
3. 認証方式と契約で提供される5時間・週間などのrate limit
4. token counter
5. Git branch
6. project rootまたはcurrent directory

必要に応じてsession IDとCodex versionを追加する。実際の識別子は対象Codexバージョンの`/statusline` pickerで選び、生成された設定をadapterの雛形へ反映する。バージョン間で識別子が変わる可能性があるため、未検証の文字列を手書きで固定しない。

CPU、メモリ、ディスク容量、コンテナ稼働時間はCodexの会話資源とは別に扱う。初期版では`agent-status`のような明示的な診断コマンドで表示し、常時表示は後からtmuxやshell promptとの統合を検討する。

## 9. Claude Codeからの移行

移行は破壊的に行わず、既存`claude-container`をrollback可能な状態で残す。

1. 既存の認証、global設定、plugins、hooks、project固有設定を分類する。
2. 秘密情報を除外し、必要なものだけagent-container専用領域へcopyする。
3. まず1リポジトリでCodexを試す。
4. clone、編集、テスト、push、PR、family Issue操作を個別に検証する。
5. Claude Codeの基本実行を同じworkspaceモデルで検証する。
6. 問題がなければ対象リポジトリを増やす。

既存ファイルのmoveや削除は、移行完了が確認されるまで行わない。

## 10. ドキュメントと言語

- 日本語を初期の正本とする
- source code、設定キー、CLI名、識別子は英語にする
- 公開時は短い英語READMEを用意できる構造にする
- 日英の完全なミラー文書は、利用者や協力者が現れるまで作らない

## 11. 実装フェーズ

### Phase 0: 設計と安全境界

- 本設計の承認
- repository構成と脅威モデル
- credentialを含めない設定例
- Codex handover/statuslineの試作

### Phase 1: Codex最小実用版

- rootless Podmanで起動
- 専用CODEX_HOMEとproject state
- GitHubからのisolated clone
- 作業ブランチ、test、push、PR
- 外部MCP経由のfamily read/Issue操作
- handover Skill、SessionStart hook、推奨statusline

### Phase 2: Claude Code基本対応

- Claude adapter
- Codexと同じworkspace・権限境界
- 既存claude-containerからの限定的なcopy移行

### Phase 3: 強化

- 開発GitHub認証の外部broker化
- CIとcross-agent reviewの標準化
- container resource監視
- 必要性が確認できた場合のみ英語文書や追加platformを拡張

#### 2026-08-29 scope変更

初期案に含めたfamily Issue create/commentは、開発repository brokerと権限を共有せず、将来Phaseのfamily専用設計へ延期する。現行interfaceは選択中repositoryのIssue list/viewだけを提供する。domain allowlist／egress controlもPhase 4には含めず、既知WARNを維持して独立した将来設計とする。

開発GitHub認証brokerの具体的なtrust boundary、Git transport、GitHub App permission、migrationと受け入れ条件は[Phase 3開発GitHub認証broker設計](2026-08-25-phase-3-github-broker-design.md)で定義する。

## 12. 初期受け入れ条件

- ホストの実`~/.codex`、`~/.claude`、開発workspaceをread-writeマウントしない
- project Aの履歴・cache・設定をproject Bから通常参照できない
- 自分の検証用repositoryで作業ブランチをpushし、PRを作成できる
- family用credentialをモデルcontextへ出さず、許可されたread/Issue操作だけを実行できる
- `main`直接push、force-push、merge、release、削除が初期標準フローに含まれない
- Codexを再起動して同じ会話をresumeできる
- 別セッションが最新handoverを発見し、必要な本文を読める
- statuslineでcontext、rate limit、token、branchを確認できる
- Claude Codeで基本的な編集・テスト・commit作業ができる
- 旧claude-containerを変更せずrollbackできる

## 13. 設計上の注意点

- コンテナは被害範囲を狭める境界であり、credentialを渡した時点で完全な秘密保持境界ではない
- agentが生成したPRは、テスト成功だけで正しいと断定しない
- ユーザー向け確認では、変更目的、動作差分、検証、残存リスクを平易な日本語で示す
- hookは補助機構であり、安全性やhandover完成をhookだけへ依存させない
- Codex CLIの新機能は変化するため、固定設定を導入する前に対象versionで検証する
