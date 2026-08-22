---
name: handover
description: Use when a user asks to create a handover, 引き継ぎ, or continuation record for a different Codex session or agent, especially when current work, verification, and uncommitted changes must be preserved outside the conversation.
---

# Handover

同じCodex会話を続けるだけなら、まず`/resume`で足りるか確認する。別セッションまたは別エージェントに事実を渡す必要があるときだけhandoverを作る。

## 手順

1. `AGENT_HANDOVER_ROOT`と`AGENT_PROJECT_ID`が設定されていることを、生値を出力せず確認する。未設定なら保存先を推測せず、ユーザーへ伝える。
2. `git status --short --branch`、直近commit、現在の計画、実行済みtestを確認する。未実行の検証を成功したものとして書かない。
3. 次のcommandで空のhandoverを作る。session IDが確実に分かる場合だけ`--session-id`を加える。

   ```bash
   PYTHONPATH=/opt/agent-container/src python3 -m agent_container.handover_cli create --root "$AGENT_HANDOVER_ROOT" --project "$AGENT_PROJECT_ID" --title "Codex作業引き継ぎ"
   ```

4. 作成されたファイルの全sectionを、現在確認できる事実で埋める。特に「決定事項と理由」「検証結果」「未解決事項とリスク」「次の一手」を具体的に書く。
5. 認証情報、PAT、API key、cookie、private key、環境変数の値を書かない。少なくとも`ghp_`、`github_pat_`、`sk-`、`BEGIN PRIVATE KEY`に似た文字列がないか確認し、見つけた場合は値を削除して種類と保管場所だけを書く。
6. `git status`とhandover本文を再確認し、commit、PR、test結果、未commit変更の記述が現状と一致することを確認する。
7. ユーザーへ保存先と、次回最初に行う一手を一文で伝える。

## 禁止事項

- transcript全文をhandoverへ貼り付けない。
- 認証情報の値を確認目的で表示しない。
- 推測した成功、commit、PR、test結果を書かない。
- 同じ会話を`/resume`できるだけの場面でhandoverを乱造しない。
