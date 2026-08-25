# Codex運用ガイド

## 普段の再開

- 同じ会話を続ける: `/rename`で名前を付け、`/resume`または`codex resume`で再開する。
- contextを整理する: `/status`で使用状況を確認し、必要なときだけ`/compact`する。
- 別セッションへ渡す: handover Skillで外部Markdownを作る。

## statusline

配布元は`profiles/codex/config.toml`。model+reasoning、context残量、primary/secondary limit、Git branch、project名の順に表示する。API情報やGit情報がない項目は表示されない場合がある。対話的な変更は`/statusline`で行う。

## Image再buildとCLI version

通常の`bin/agentctl build`はCodexとClaude Codeのversion既定値を両方`latest`としてnpmへ解決し、毎回変わるcachebusterでCLI install layerをinvalidateする。そのため、通常buildはその時点で公開されている両CLIの最新versionを取得し、終了時に解決した公開versionを表示する。runtimeのself-updateは無効であり、version変更はimage再build時だけに起きる。

`--codex-version VERSION`と`--claude-version VERSION`は問題調査またはrollback専用である。通常の更新では固定versionを渡さない。

## handover保存境界

launcherは対象projectについてだけ`AGENT_PROJECT_ID`と`AGENT_HANDOVER_ROOT`を設定する。Obsidian vault全体をmountしない。handover本文にはcredentialの値を残さない。

## 起動hook

`profiles/codex/hooks.json`を専用`CODEX_HOME`へ配布する。初回または定義変更後は`/hooks`でcommandを確認してtrustする。hookは最新handoverのpathだけを通知し、本文は必要なときにCodexが読む。

Claudeのmanaged sandboxでは、初期状態のhooksとMCPをEnterprise policyで無効にしている。これはCodexのhook設定とは別の境界であり、Codexのhandover通知hookを無効化するものではない。Claude側の制約とsecurity gateは[Phase 2運用ガイド](phase2-claude-code.md)を参照する。

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

## 検証記録

2026-08-22に、clean shellと一時的な専用test環境で確認した。

- Python: `Python 3.14.6`。
- Codex: `codex-cli 0.149.0`。通常の`codex --version`では、PATH aliasをread-only filesystemへ作成できないというwarningが出たが、version表示は成功した。このwarningは検証失敗ではない。
- 自動test: `PYTHONPATH=src python3 -m unittest discover -s tests -v`はexit 0で、18 testすべて`ok`（`Ran 18 tests in 0.135s`, `OK`）。
- hook本文非注入: 一時handover rootに`DO-NOT-INJECT-BODY`だけを含む最新handoverを作り、`SessionStart` JSONを`handover_hook`へ渡した。exit 0で最新handoverのpathを含むJSONのみを返し、本文文字列は出力に含まれなかった。
- path traversal拒否: parent一時fixture内の`root`を`--root`に指定し、known siblingの`outside`を未作成のまま`handover_cli create --project ../outside`を実行した。exit 1で`project_id must be a single safe repository-style slug`を返し、実行後もそのknown sibling targetは存在しなかった。
- strict config: `config.toml`と`hooks.json`を一時`CODEX_HOME`へコピーして`CODEX_HOME=... codex --strict-config --version`を実行し、exit 0で`codex-cli 0.149.0`を返した。strict-config errorはない。temporary directory配下へPATH aliasを作成しないというCodex warningは出たが、real host Codex profileは使用も変更もしていない。
- handover Skillのfresh-agent評価: SkillなしのREDではagentが`.codex/HANDOVER.md`を推測しproject CLIを省略した。SkillありのGREENではstorage変数不足を拒否し、CLI contractを使用、環境値とfull transcriptを除外し、未確認のGit/testsをskippedとして記録した。

認証済み専用containerでのPhase 1 integration checkは2026-08-22に完了した。rootless Podman、device auth、private clone、authenticated TUIでのhook trust（`/hooks`）、statusline、`/status`、session resume、共有認証の継続、test branchのpushと未merge PR作成までの結果は、承認付きの[Phase 1 smoke test checklist](phase1-smoke-test.md)に記録している。

2026-08-24のPhase 2 regressionでは、最新rebuild済みimage上の`bin/agentctl doctor agent-container --agent codex`がexit 0（既知のnetwork-policy WARNのみ）、Codex `0.149.1`の認証済みTUIが起動し、SessionStart hookがhandover本文ではなくpathだけを通知した。sandbox内shellは`CODEX_SMOKE_OK`を返し、通常終了後に同じprojectの直前sessionを`/resume`で再開できた。workspaceの変更、push、PR、mergeは行っていない。`codex_apps` connectorは保存token期限切れのHTTP 401で起動しなかったが、Codex本体、shell、handover hook、resumeの回帰結果とは分離して記録する。

prototypeのunit testと実host smoke testは役割が異なる。前者は安全境界とcommand contractの回帰を検出し、後者は認証済みTUIと外部サービスを含むend-to-end動作を確認する。
