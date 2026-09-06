# Claude strong nested sandbox・global scrub採用設計

日付: 2026-09-06

## 背景

[2026-08-24の設計](2026-08-24-debian-project-images-claude-sandbox-design.md)は、Claude Codeのnested sandboxについて「Outer Podman + weaker nested sandbox + scoped credential policy」を採用し、「Global scrub + strong nested sandbox」（option 2）を「現在のrootless Podmanではprocfs mountが拒否される」という理由だけで不採用とした。同設計は「Anthropicまたはruntime側の変更があれば再検証する」と記録している。

PR #108（2026-09-06）でagent runtime containerに`--security-opt=unmask=/proc/*`を追加し、bubblewrapがuser namespace内で新しい`/proc`をmountできるようになった。これはClaude Code文書が`enableWeakerNestedSandbox`を必要とする条件（unprivileged container内で`Can't mount proc`となる場合）そのものであり、option 2を不採用にした前提が消えた。

2026-09-06のspike（使い捨てimage、`enableWeakerNestedSandbox: false`と`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`、認証済み非対話`claude -p`、project `agent-container-claude-smoke`）で次を観測した。

- sandbox内Bashが動作し、bwrapの`/proc` mount失敗が無い。
- 専用probeは`oauth_token_visible=false`、`token_file_readable=false`、`parent_token_via_proc_readable=false`。
- sandbox内の`/proc`に数字entryが7件だけで、containerの他process（親Claude processを含む）が見えない。
- 子processで`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`が観測できる。
- `/run/agent-handover/broker.sock`へのAF_UNIX接続が成功する。

weaker modeはcontainerの既存`/proc`をinner sandboxへbindするため、親Claude processの環境が構造的には見える。現在の防御はprobeの`parent_token_via_proc_readable=false`という観測に依存している。strong modeは新しいPID namespaceとprocfsで親processを隠し、global scrubはsandbox設定に依存せず全subprocessからcredentialを除去する。

## 目標

- Claude runtimeのnested sandboxをstrong mode（`enableWeakerNestedSandbox: false`）にし、Claude launcherが`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定する。
- 既存の防御（credential deny list、token file deny、built-in Read deny、`allowAllUnixSockets`、`failIfUnavailable`、unsandboxed command禁止、hooks／MCPのmanaged限定）を維持する。
- managed policyのexact-match validator、image test、launcher testが新しい設定を固定する。
- 実hostの対話TUI gateで、sandbox・probe・handover create・Family intakeが新しい設定で動くことを確認してから完了とする。

## 非目標

- weaker modeをproject別や環境変数で選べるfallbackとして残すこと。sandboxを弱めるfallbackは設けない。
- stdio MCPやproject hooksの再有効化。global scrubで保護対象になるが、有効化は別のsecurity reviewで扱う。
- Codex側のsandbox設定変更。Codexは自身のbubblewrap sandboxを使い、PR #108／#109で扱い済み。
- 外側Podman制約（`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、mount構成、`/proc` unmask以外のmask）の変更。

## 比較した方式

### A. strong nested sandbox + global scrubへ全面切替、fallbackなし（採用）

managed policyの`enableWeakerNestedSandbox`を`false`にし、launcherが`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定する。`failIfUnavailable: true`を維持するため、sandboxを起動できない環境ではClaude起動が失敗し、weaker modeやunsandboxedへ戻らない。validatorは新しい設定を厳密照合する。2026-08-24設計のoption 2をそのまま採用する。

### B. weaker modeをfallbackとして残す

「sandboxを弱めない・fallbackしない」という既存方針に反し、exact-match validatorで固定できない設定variantが増える。不採用。

### C. `enableWeakerNestedSandbox`だけをfalseにする

subprocessのcredential除去とshell-mode commandのsandbox化が得られず、option 2の半分にしかならない。不採用。

## 設計

### Managed policy

`profiles/claude/managed-settings.json`と`src/agent_container/claude_policy.py`の`EXPECTED_SETTINGS`で`sandbox.enableWeakerNestedSandbox`を`false`にする。他のkey（`enabled`、`allowUnsandboxedCommands: false`、`failIfUnavailable: true`、`network.allowAllUnixSockets: true`、`credentials`のenvVars／files deny、`permissions.deny`、`disableBypassPermissionsMode`、`allowManagedHooksOnly`、`allowedMcpServers: []`、`allowManagedMcpServersOnly`、`statusLine`）は変えない。validatorはfile mode、owner、byte上限の検査を維持し、`true`を含む旧policyを拒否する。

### Launcher

`claude_launcher.exec_claude`は環境から`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`をpopする代わりに`"1"`を設定する。`IS_DEMO=1`、`CLAUDE_CODE_OAUTH_TOKEN`の親process限定の受け渡し、workspace trust seed、`O_NOFOLLOW`でのtoken読み取りは不変。Claude Code文書によれば、この変数が設定されていると全subprocessからcredentialが除去され、filesystem isolationはどのsourceからも無効化できず、Linuxではshell-mode commandを含む全commandがsandbox内で動く。

### Runtime

`run_claude_spec`のPodman引数は変えない。PR #108の`--security-opt=unmask=/proc/*`がstrong modeの前提（bwrapのuser namespace内での新しい`/proc` mount）を満たす。

## Security acceptance gate

merge後、専用imageを再buildし、利用者のprivate terminalから対話TUIで次を確認する。いずれかが失敗したら停止し、sandbox、mount、policyを弱めて再試行せず、PRのrevertを検討する。

- `/sandbox`がmanaged設定で固定されている（local変更不可の表示）。`/hooks`と`/mcp`は既知のblock済みplugin hook 1件（`findsummits`のみ）と0件を除き増えていない。
- sandbox内Bashから`python3 -m agent_container.claude_security_probe`がexit 0で3項目`false`だけを出力する。
- sandbox内の`/proc`に親Claude processが見えない（数字entryがsandbox内processだけ）。
- `agent-handover create --title TITLE < FILE`がbroker経由で成功し、stdoutがpathだけで、host側fileがmode 600・canonical metadata・7 sectionを持ち、auditが固定fieldの`create`／`ok`／`write` 1行だけ増える。global scrubが`AGENT_HANDOVER_BROKER_SOCKET`／`AGENT_HANDOVER_BROKER_CAPABILITY`などの非credential環境変数を落とさないことの確認を兼ねる。
- Family bindingのあるprojectで`agent-family issue create`の1回目が`pending`、同runの2回目が拒否される。
- 通常終了後にcontainer、socket、capabilityが消える。

観測ではcredential値、環境一覧、`/proc/*/environ`本文を表示・記録しない。

## Error handling

- sandboxを起動できない場合は`failIfUnavailable: true`によりBash commandが失敗し、unsandboxed fallbackは`allowUnsandboxedCommands: false`で禁止されたままである。runtime側でweaker modeへ戻す経路は設けない。
- policy validatorはimage内で完結する。image自身が配布する`managed-settings.json`をimage自身が持つ`EXPECTED_SETTINGS`と比較するため、この修正を含まない古いimageは両者とも旧設定（`true`）のままで一致し、`doctor`の`claude-managed-policy`と`run`前の検査はFAILにならない。strong modeとglobal scrubを有効にするには、この修正を取り込んだ状態で`bin/agentctl build`によりimageを再buildする必要がある。validatorが検出できるのは、再buildしたimage内で`managed-settings.json`が改ざん・破損している場合だけである。

## Testing strategy

### Unit tests

- `tests/container/test_claude_policy.py`: 期待設定が`false`であること、`true`を含むpolicyを拒否すること（既存のmutation caseの反転）。
- `tests/container/test_image.py`: 配布する`managed-settings.json`の`enableWeakerNestedSandbox`が`false`。
- `tests/container/test_claude_launcher.py`: `exec_claude`が`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定し、tokenは親processだけへ渡す（既存testの反転）。
- `tests/container/test_docs.py`: 運用文書の文言（global scrubを設定すること、strong modeであること、probe必須）を固定する。

### Real Podman integration test

`tests/integration/test_agent_sandbox_podman.py`に、`run_claude_spec`の実argv（`--interactive`／`--tty`除去、agent commandを`bwrap --unshare-user --unshare-pid --proc /proc --ro-bind / / --dev /dev /usr/bin/printf TOKEN`へ置換）でexit 0と出力一致を確認するtestを1件追加する。Claude本体は認証を要するためCIでは前提条件（新しい`/proc`をmountできること）だけを固定する。CIのPodman gateの固定件数を16から17へ更新する。

### Live smoke

上記Security acceptance gateを実施し、結果をCHANGELOG Validationと`docs/phase2-smoke-test.md`の観測表へ記録する。

## Documentation

- `docs/phase2-claude-code.md`: global scrubを設定しない理由、weaker mode前提、「親Claude processが既存`/proc`に見える可能性」の記述を、strong modeとglobal scrubの記述へ置き換える。probe必須と停止条件は維持する。
- `docs/phase2-smoke-test.md`: 手順3の期待値を「`enableWeakerNestedSandbox`が無効」に更新する。
- 2026-08-24設計: option 2を本設計で採用した旨を「Existing designとの関係」に追記する。
- README: security boundary節のnested sandbox記述。
- CHANGELOG: Security boundaries（`/proc`可視性の縮小、subprocessのcredential除去）とChanged。
- `docs/family-issue-create-broker-smoke-test.md`と`docs/superpowers/specs/2026-09-04-broker-kernel-design.md`: 固定件数の更新（既存の件数例外規則）。

## Existing designとの関係

- 2026-08-24設計の「Security acceptance gate」6項目は本設計でも全て必須とし、strong modeで親processが構造的に見えないことを追加で確認する。
- 2026-08-23のsetup-token設計が求めた`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`の必須化が、本設計で実現する。

## Completion criteria

1. managed policy、validator、launcher、testsを変更したPRのrequired CI（unit、Podman 17件）が成功し、mainへmergeされている。
2. 再buildした専用imageで、Security acceptance gateの全項目を実hostの対話TUIで確認し、日付・image ID・固定fieldだけをCHANGELOG Validationに記録している。
3. 運用文書からweaker mode前提の記述が消え、probe必須と停止条件が維持されている。
