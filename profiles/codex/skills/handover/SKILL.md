---
name: handover
description: Use when a user asks to create a handover, 引き継ぎ, or continuation record for a different Codex session or agent, especially when current work, verification, and uncommitted changes must be preserved outside the conversation.
---

# Handover

別セッション・別agentへ現在の事実を渡すための手順。同じ会話を続けるだけなら `/resume` を使う。

## 作成手順（profile version 5）

1. `AGENT_HANDOVER_ROOT`、`AGENT_PROJECT_ID`、`AGENT_HANDOVER_BROKER_SOCKET`、`AGENT_HANDOVER_BROKER_CAPABILITY`の設定有無を、生値を表示せず確認する。broker capabilityの本文を読まない。欠落時は作成を止め、imageと `project update-profile PROJECT` の整合を確認する。保存先を推測しない。
2. `git status --short --branch`、直近commit、現在の計画、実行済みtestを確認する。未実行は `not run` と理由を書く。
3. 完成本文を、自分で作成したmode `0600`の一時fileへ用意する。作成例:

   ```sh
   umask 077
   handover_body=$(mktemp /tmp/agent-handover-body.XXXXXX.md)
   ```

   本文は下記7 headingを各1回、固定順で含む。すべてのsectionを現在確認できる事実で埋める。H1、Project、Created、Sessionのmetadataは書かない。hostが生成する。

   ```markdown
   ## 作業の目的
   ## 現在地
   ## 決定事項と理由
   ## 変更したファイル・commit・PR
   ## 検証結果
   ## 未解決事項とリスク
   ## 次の一手
   ```

4. `CODEX_SESSION_ID`が設定済みなら、改行・制御文字・credentialらしい値でないことを確認し、「現在地」sectionに `Codex session ID（agent申告・host未検証）: 値` と記録する。未設定なら `Codex session ID: 未設定（host未検証）` と書く。不正な値は転記せず、記録できない理由を書く。これは会話再開用の申告値で、真正性をhostが確認したIDではない。canonical `Session`欄は `（未記録）` が正しい。
5. 本文・title・申告IDに認証情報、PAT、API key、cookie、private key、他の環境変数の値、transcript全文がないことを確認する。`ghp_`、`github_pat_`、`sk-`に続くtokenらしい値や `BEGIN PRIVATE KEY` などがあれば値を除き、種類と保管場所だけを書く。
6. 完成本文を次の専用commandへ一度に送信する。`handover_body`は作成済み一時fileのpath。別のtool呼び出しへ移ったら、その自分のfileのpathを使う。shell変数が保持されると仮定しない。

   ```sh
   agent-handover create --title "Codex作業引き継ぎ" < "$handover_body"
   ```

   titleだけで空文書を作らない。保存済みfileへの追記・編集はしない。保存先・project・sessionの追加optionや別のwriterを使わない。
7. exit 0で返ったpathを読み直し、title、Project、canonical `Session: （未記録）`、必須7 section、送信本文、申告IDの表記、Git・commit・PR・test事実を照合する。保存先がread-onlyであることは正常。読み直せない・一致しない場合は保存確認未完了として報告する。成功確認後に自分の一時本文fileを削除し、保存先と次回最初の一手を伝える。

## 失敗時の分岐

- **brokerを使う現在の経路**: 拒否・不通・設定欠落なら保存未完了。直接write、read-write mount、workspaceへの代替handoverで回避しない。固定errorと設定有無から原因を調べ、必要ならoperatorにdoctor確認を求める。応答が不明な場合は、対象projectのhandover directoryを通常の読み取り・一覧commandで確認し、最新文書のtitleと本文を照合してから再送を判断する。新しいbroker read operationは必要ない。手元の一時本文は未公開の下書きであり、正式保存先として報告しない。
- **tool sandboxの制約**: `Read-only file system`や接続拒否だけでPodman mountやhost directoryの障害と断定しない。既存承認とpolicyが許す同じ専用commandの実行経路を確認する。sandbox外実行が許される場合も、broker自身の拒否を別writerで回避しない。自動承認reviewが拒否した場合はその理由を報告する。
- **移行前と確認できた旧direct経路**: broker環境がないことだけでは旧版と断定しない。旧imageと空文書作成後の編集手順を確認済みなら、通常sandboxのread-only errorに対して、既存承認とpolicyが許す専用commandのsandbox外実行を使う。この旧版ではtitleだけで空文書を作り、返却fileの既存metadataを保持して7 sectionを直接埋める。stdin送信では本文は保存されない。設定済みIDは旧版のcanonical Session欄にあることも含め、本文の保存・再読まで確認する。許可がなければ未完了を伝える。正式保存先を書けないと即断してworkspaceに代替保存しない。次回run前に対応imageとprofile version 5へ更新する。
