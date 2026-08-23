# Phase 2 Claude Code setup-token認証 再設計

**作成日:** 2026-08-23  
**対象:** `agent-container`  
**状態:** ユーザー承認済み

## 1. 位置づけ

この文書は、`2026-08-23-phase-2-claude-code-design.md`のClaude認証、永続状態、runtime mount、doctor、host smokeに関する規定を置き換える。Codex、rootless Podman、project workspace、handover、GitHub、migrationの境界は維持する。

実host smokeで、共有Claude config directoryを使う`claude auth status`は成功する一方、project別`CLAUDE_CONFIG_DIR`へ共有`.credentials.json`だけをnested mountした構成ではstatusが失敗した。Claude Codeは認証判断に`.claude.json`内の`oauthAccount` metadataも使用し、nested credential mountは認証更新時のatomic replaceとも両立しない。このため、credential file-only共有方式を廃止する。

## 2. 目的

- Claude subscription用の認証をagent-container専用領域で一度だけ設定する。
- 認証をproject間で共有しながら、Claudeの設定、session、履歴、plugins、memory、cacheをproject別に隔離する。
- credentialをPodman argv、host環境、log、doctor、handover、ClaudeのBash/hooks/MCP subprocessへ露出させない。
- credential更新にnested bind mountやprojectごとのcredential copyを使わない。
- 既存Codex運用とruntime保護を維持する。

## 3. 非目標

- Claude Remote Control。`setup-token`はinference専用であり、Remote Controlには使用しない。
- host `~/.claude`、旧`claude-container`、他project stateのmount。
- tokenの自動取得、画面scraping、clipboard操作。
- API key、Bedrock、Vertex、Foundryを既定認証にすること。
- legacy credential本文の読取り、表示、移行。

## 4. 永続状態

```text
<state-root>/
├── shared-auth/
│   ├── codex/
│   │   └── auth.json
│   └── claude/
│       └── oauth-token
├── projects/<project>/
│   ├── codex-home/
│   ├── claude-config/
│   ├── cache/
│   └── project.json
└── workspaces/<project>/
```

`shared-auth/claude`は`0700`、`oauth-token`は`0600`、通常file、非symlink、実行user所有とする。token fileは単一行の印字可能ASCII、32〜4096 bytes、空白・control characterなしとし、値をerrorへ含めない。

Claude runtimeが読む共有認証情報は`oauth-token`だけとする。project `claude-config`に`.credentials.json`が存在した場合は、空fileでも起動前にFAILとする。project `.claude.json`はproject固有stateとして許可し、共有領域へ同期しない。

## 5. CLI contractと認証フロー

利用者向けcommandは維持する。

```bash
bin/agentctl auth claude
```

処理順は次のとおり。

1. state root、既存token metadata、rootless Podman、imageを検証する。
2. credentialを永続化しないtmpfs上の`CLAUDE_CONFIG_DIR`で、hardened containerの`claude setup-token`を対話実行する。
3. 上流CLIはprivate terminalへtokenを一度表示する。agentctlは表示内容をcapture、parse、再表示、log保存しない。
4. container終了後、agentctlはhostのhidden promptでtoken貼付けを受ける。端末echoを無効化する。
5. tokenを同一directoryのprivate staging fileへ排他的に保存し、formatとmetadataを検証する。
6. staging tokenをread-only mountしたruntime launcherで`claude auth status`を実行し、exit 0を必須とする。stdout/stderr本文は表示しない。
7. 成功時だけ`oauth-token`へatomic replaceする。失敗・取消時は既存tokenを変更しない。

tokenをcommand option、stdin pipe、environment引数、JSON、test fixture、handoverで受け渡さない。`setup-token`がtokenを表示する操作は、利用者同席のprivate terminalでのみ許可し、screen recordingや会話logへ残さない。

## 6. Runtime launcher

Claude runtimeは次だけをmountする。

| Host source | Container target | Mode |
|---|---|---|
| project workspace | `/workspace` | read-write |
| project `claude-config` | `/home/agent/.claude` | read-write |
| shared `oauth-token` | `/run/secrets/claude-oauth-token` | read-only |
| project cache | `/home/agent/.cache` | read-write |
| dedicated GitHub config | `/home/agent/.config/gh` | read-only |
| project handover | `/handovers/<project>` | read-write |

image内に専用launcherを置く。launcherはsecret fileを`O_NOFOLLOW`で開き、通常fileとformatを再検証し、tokenを`CLAUDE_CODE_OAUTH_TOKEN`として自process環境へ設定する。同時に`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を必須設定し、`claude`へ`exec`する。

tokenはPodman argvとhost環境に入れない。launcherはtoken、environment、file本文を出力しない。ClaudeのBash、hooks、MCP stdio serverへcredentialを継承させず、Linux subprocessのPID namespace分離を有効にする。

runtimeは引き続きrootless Podman、`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、bounded `/tmp` tmpfsを使用し、permission bypass optionを渡さない。

## 7. Doctor

Claude doctorは次を確認する。

- image内Claude version
- shared `oauth-token`の存在、owner、mode、通常file、非symlink、safe format
- launcherを使った`claude auth status`のexit 0。stdout/stderrは破棄する
- project `claude-config`のowner、mode、非symlink
- project config内に`.credentials.json`が存在しないこと
- workspace origin、handover project境界
- Phase 2 network-policy WARN

token本文、token prefix、期限、account metadata、environment値は表示しない。

## 8. Legacy stateと回復

新tokenのstatus確認前にlegacy stateを変更しない。新方式が成功した後、旧shared `.credentials.json`、shared `.claude.json`、auth backupsをprivate quarantineへ移動する。quarantineはstate root内、directory `0700`、file `0600`とし、自動削除しない。

failed runtime smokeでproject configに生成された`.credentials.json`もquarantineへ移す。project `.claude.json`、project backups、project cacheはproject固有stateとして残してよい。quarantine対象は事前にmetadataとexact pathを確認し、本文を読まない。

rollback時は新token fileをmountしない旧image/branchへ戻せる。legacy quarantineの復元は自動化しない。

## 9. Error handling

- setup-token失敗、hidden prompt取消、token format不正、status失敗では既存tokenを変更しない。
- token staging、validation、atomic replaceのfilesystem errorは一般化し、pathや本文を出さない。
- token fileやproject configがsymlink、境界外、不正mode、不正ownerならcontainerを起動しない。
- project `.credentials.json`があればruntimeを起動しない。
- launcherがsecretを読めない、subprocess scrubが無効、status/inferenceが失敗する場合はPhase 2未完了として停止する。
- `CLAUDE_CODE_OAUTH_TOKEN`をprocess treeの診断出力やdebug logへ含めない。

## 10. Test strategy

### Automated

- parser/state path/token validation tests
- setup-token container specがtmpfs configのみを使い、host configやtokenをargvへ含めないこと
- hidden token readerのechoなし、取消、invalid token、既存token preservation
- staging token status成功後だけatomic replaceすること
- runtimeがtoken fileをread-only mountし、`.credentials.json`をmountしないこと
- launcherがtokenをargv/stdout/stderrへ出さず、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定すること
- doctorがtoken本文とenvironment値を出さないこと
- project credential fileを事前拒否すること
- Codex regressionと全test suite

secret fixtureは値をassert失敗へ出さない。subprocess testはcredentialの「存在しない」というbooleanだけを確認する。

### Host smoke

1. 利用者同席のprivate terminalでsetup-token認証を完了する。
2. launcher経由の`auth status`を本文なしで確認する。
3. 実際の最小inferenceを行い、statusだけ成功してAPI callが失敗するケースを除外する。
4. Claude subprocessからOAuth tokenが見えないことをbooleanで確認する。
5. project configにcredential copyがないことを確認する。
6. Claudeでnon-main branchの小変更、test、local commitを行う。
7. 終了・再起動・resumeを確認する。
8. Codex regressionと旧claude-container無変更を確認する。
9. 成功後にlegacy stateをprivate quarantineへ移し、再度doctor/runtimeを確認する。

push、PR、merge、force-push、release、削除はsmokeに含めない。

## 11. 完了条件

- token認証でClaude runtimeと最小inferenceが成功する。
- Claude subprocessとproject stateへcredentialが露出しない。
- project別session/config分離とresumeが成立する。
- Codexの既存build/auth/run/doctorが回帰しない。
- legacy credentialは削除せずprivate quarantineへ隔離される。
- automated tests、host smoke、whole-branch reviewが成功する。

