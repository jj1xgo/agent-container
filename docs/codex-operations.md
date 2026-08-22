# Codex運用ガイド

## 普段の再開

- 同じ会話を続ける: `/rename`で名前を付け、`/resume`または`codex resume`で再開する。
- contextを整理する: `/status`で使用状況を確認し、必要なときだけ`/compact`する。
- 別セッションへ渡す: handover Skillで外部Markdownを作る。

## statusline

配布元は`profiles/codex/config.toml`。model+reasoning、context残量、primary/secondary limit、使用token、Git branch、project名の順に表示する。API情報やGit情報がない項目は表示されない場合がある。対話的な変更は`/statusline`で行う。

## handover保存境界

launcherは対象projectについてだけ`AGENT_PROJECT_ID`と`AGENT_HANDOVER_ROOT`を設定する。Obsidian vault全体をmountしない。handover本文にはcredentialの値を残さない。

## 起動hook

`profiles/codex/hooks.json`を専用`CODEX_HOME`へ配布する。初回または定義変更後は`/hooks`でcommandを確認してtrustする。hookは最新handoverのpathだけを通知し、本文は必要なときにCodexが読む。

## 手動確認

1. test用のhandover rootとproject IDを設定する。
2. `agent_container.handover_cli create`でhandoverを作る。
3. `handover_hook`へ`SessionStart` JSONを渡し、本文ではなくpathだけが返ることを確認する。
4. 専用`CODEX_HOME`でCodexを起動し、`/hooks`でhookをtrustする。
5. `/statusline`で設定項目と順序を確認する。
6. `/status`でcontextとrate limitの表示を確認する。

## 障害時

- hookが動かない: `/hooks`でsource、hash、trust状態を確認する。
- handoverが見つからない: `AGENT_PROJECT_ID`と狭くmountしたhandover directoryの対応を確認する。
- statusline項目が欠ける: 認証方式、APIデータ、Git repository内かどうかを確認する。
- 古いhandoverが出る: filenameが`YYYY-MM-DD_HHMM.md`であることとproject IDを確認する。
