# Codex handover broker統一の検証記録

対象: [Issue #120](https://github.com/jj1xgo/agent-container/issues/120)。基準mainは `74fc0d7`（PR #122）。[設計](superpowers/specs/2026-09-07-codex-handover-broker-design.md)の申告session ID案を2026-09-07に利用者が承認した。Phase 6の保守であり、Phase 7は未着手。

## 現在の判定

実装・ローカル検証・独立のブランチ全体reviewは完了。2026-09-07、head `636128c8b9c783478807fe32cde1582ae606d82f` のrequired CIはUnit tests／Podman integrationともSUCCESS（run `34111733529`、attempt 2）を確認した。同runのattempt 1のegress TLS timeout／Family PID取得失敗の原因は未確定であり、再実行成功で解決済みとは扱わない。

同日、利用者の「Issue #120 の実ホスト検証を進める」という依頼に基づき、専用projectで認証済みCodex／Claudeの6 gateすべてがPASSした。詳細は下記「2026-09-07 実ホスト検証」。これは非対話CLIから通常sandboxのtoolを実行した結果であり、対話TUIの操作確認ではない。実host gateは満たした。merge／Issue close／Phase 7着手はこの検証作業に含めていない。

## 対象と境界

- Codex／Claudeともproject handoverはread-only mount、完成した7 sectionをstdinでcreate-only brokerへ送る。
- hostはproject、Created、filename、atomic publishを決める。canonical Sessionは `（未記録）`。
- Codex skillは会話IDを本文に `Codex session ID（agent申告・host未検証）: …` と記録する。未設定は `Codex session ID: 未設定（host未検証）`。これはhostが真正性を確認したmetadataではない。
- wire version 1、既存audit golden、Claudeの文書契約、host writerは保持する。
- profile version 5へ更新し、旧profileのrunを拒否する。setupの新規既定はstate path末尾の `/` を除いて `-handovers` を付けた保存先。既存の重複登録・文書はoperatorによる移行が必要で、自動移動しない。

## ローカル検証

| 確認 | 対象 | 結果 |
| --- | --- | --- |
| 基準Codex unit | main `74fc0d7` | 49件PASS。実装後の検証ではない |
| 基準container unit | main `74fc0d7` | 1169件PASS。実装後の検証ではない |
| wrapperと実Unix socket | 今回のwrapper、既存broker | session設定済み／未設定の両方で本文一致、canonical Session未記録、mode 0600、staleとbroker環境欠落を拒否。1 test・2 subcase PASS |
| 旧wrapperによる再現 | `74fc0d7:container/bin/agent-handover`を独立一時directoryで使用 | 同じtestが両subcaseで期待どおり失敗。broker環境欠落でdirect writerがexit 0となりfileを作成した。現worktreeのwrapperは差し戻していない |
| 実装後全unit・lint・socket | `f966e64`の実装と同時点の統合fixture | container 1178件、Codex 49件、CI broker socket 9件PASS。ResourceWarningなし。lint PASS。内部validator改名後は対象7件を再実行してPASS |
| Podman fixture準備 | 今回の統合test | 両sandbox fixtureで実brokerのmount作成・cleanupをローカル確認。probe Python／launcher shell構文、Ruff PASS。CI対象17件のdiscoveryを確認（未実行）。network testを拡張し、outer read-only mountのEROFSとCodex tool sandbox内の拒否・broker create・再読を分けて検査する |
| 文書／skill独立review | `74fc0d7..cd87377`とmigration節 | 指摘なし。更新skillの判断評価は下記 |
| ブランチ全体独立review | `74fc0d7..cf50a07` | `gpt-6-astra`／high、指摘なし。runtime、profile移行、session表記、統合testを確認。実行gateの代替ではない |
| required CI | [PR #123](https://github.com/jj1xgo/agent-container/pull/123/checks) | Unit tests／Podman integrationの最新headの結果をPR checksに集約する。ローカル結果で代用しない |
| 実Podman | CIの専用image | 実装時のcontainer workspaceではnot run（Podmanなし）。CIのPodman integrationで17件を検証し、実行結果は上記PR checksを参照 |
| 認証済み両agentの実host gate | head `636128c`、専用host image/profile v5 | 2026-09-07、両agentの6 gate PASS。下記参照 |

## Skillの振る舞い評価

実行日: 2026-09-07。別contextのsubagentに同じ4 scenarioを渡し、次のcommand・payload・再読確認・報告を選ばせた。モデルは `gpt-5.6-sol`、reasoningはmedium。これは模擬判断の評価であり、実際のtool sandboxやhostへの保存成功を示さない。各条件は1 sampleであり、モデル間比較や成功率の推定には使わない。

### シナリオ

A. 別agentへのhandoverが必要、context残量が少なく作業は長時間。7 sectionは準備済みでtestsはnot run。保存後のfileはread-only。合成session IDは `00000000-0000-4000-8000-000000000123`。

B. 旧direct経路で通常tool sandboxからの専用commandが `Read-only file system`。workspaceは書ける。専用commandのsandbox外実行は利用者承認済みでpolicy review可能。Podman／host側の保存可否は未確認。

C. broker環境設定済み、完成本文送信後にexit 1・固定error・stdout空。利用者は退出を急ぎ、workspaceは書ける。

D. Aと同じだがsession ID環境は未設定。

### 観測

| 条件 | A | B | C | D |
| --- | --- | --- | --- | --- |
| guidanceなし | title-only commandを選び、canonical session metadataを期待。stdin手順が具体化されない | 承認済みのsandbox外専用commandへ進む | `/workspace/HANDOVER-broker-fallback.md`への代替保存を選ぶ | canonicalをnullまたは省略と推測 |
| 旧配布skill | title-only作成後の編集がread-onlyでできず未完了 | 専用commandをsandbox外で実行し本文を編集する | 未公開と報告し代替保存しない | 未設定時のcanonical表現を決められない |
| 更新skill・初回 | stdin送信、本文の未検証ID、canonical未記録、再読を選ぶ | 旧directにもstdinを適用する誤り | 代替保存しないが通常のdirectory読み取りを明示できない | 未設定の本文とcanonical未記録を選ぶ |
| 更新skill・修正後 | 環境の設定有無を確認しstdin送信、本文とmetadata再読 | 旧版のtitle-only→metadata保持→本文編集→再読を選ぶ | read/listで正式保存先を確認し重複を避ける。workspace代替保存なし | 未設定の本文とcanonical未記録を分けて照合 |

初回の誤りに対し、旧directではtitle-only作成後にmetadataを保持して本文を埋めること、stdinでは保存されないことを追記した。broker応答不明時は通常のread/listで最新文書を確認できることも明記した。修正後の独立context評価では4 scenarioとも意図した分岐を選んだ。実行を伴わない1 sampleの判断評価なので、実host gateや一般的な成功保証には用いない。通常sandboxのread-only事例は、controlと旧版もhost障害と即断しなかったため、このscenarioで誤認率が改善したとは主張しない。

## 実host gateへの対応付け

[Phase 2 smokeのhandover gate](phase2-smoke-test.md#claude-handover-brokerのmerge後gate)は既存のまま保持する。今回のCodexへの適用は下表で追加管理する。元手順の実host・認証済みCLI・disposable project・個別承認という前提を維持し、fixture以外の既存文書を変更しない。

| 元手順 | Codex経路の確認 | Claude回帰 | 2026-09-07 host |
| --- | --- | --- | --- |
| 1 create | 完成本文、pathのみ、mode 0600、canonical未記録と本文の申告ID | 既存文書契約と7 section一致 | 両agent PASS |
| 2 直接変更拒否 | create/write/overwrite/rename/delete拒否、fixtureのhash不変 | 同じ拒否と不変 | 両agent PASS |
| 3 project境界 | 他project mount不在、別ID request拒否 | 同じ拒否 | 両agent PASS |
| 4 content policy | malformed／安全なdummy marker拒否、final/temp増加なし | 同じ拒否 | 両agent PASS |
| 5 non-logging | stdout/stderr/auditへ本文・title・capabilityを出さない | 同じ非露出 | 両agent PASS |
| 6 終了後失効 | socket/capability/run directory不在、stale拒否、audit不変 | 同じcleanup | 両agent PASS |

通常Codex tool sandbox内からのstdin送信も確認する。sandbox外commandの成功だけを通常tool sandboxの成功としない。起動／停止／再試行のunit・socket結果と、認証済みTUI結果も区別する。

## 2026-09-07 実ホスト検証

### 対象と準備

- host worktree: `/home/tsu/Projects/agent-container/.worktrees/codex-handover-broker`、branch `feat/codex-handover-broker`、実装head `636128c8b9c783478807fe32cde1582ae606d82f`。実装ファイルの変更なし。
- `bin/agentctl --image localhost/agent-issue120-test:local build` はexit 0。version固定optionなし。image ID `e28bd24e7adba28b05326dd3373c9e4a9822ab7e6940a2c9ab6c0004d43789f3`、Node `v26.8.1`、Codex `0.153.4`、Claude Code `2.1.263`。
- 新規disposable project `issue120-smoke`、repository `jj1xgo/agent-container-smoke`。project専用workspace/profile v5と `/tmp/issue120-handovers/issue120-smoke` を使用。既存projectの移行・profile更新・handover移動なし。共有認証は既存の管理済み認証経路を使い、credential本文の表示・複製なし。
- 両agentのdoctorはexit 0、必須項目すべてPASS。既知のWARNは外向き通信がdomain制限なしであること。今回の専用projectにegress policy／Family bindingを追加していない。
- mode 0600の固定probe、完成済み7 section本文、合成titleと既存文書fixtureを準備。本文中のCodex session IDは合成値であり、実会話IDの取得・真正性確認の試験ではない。

### 方法と結果

通常の `agentctl main`／production runtime builderを使い、末尾のagent commandだけをCodex `exec --ephemeral --json`、Claude `--print --output-format stream-json --no-session-persistence`へ拡張した。containerのmount／sandbox／認証境界はproduction builderのまま。各agentへ `python3 /workspace/issue120-probe.py` を通常toolで1回だけ実行するよう指示し、tool resultとhost側の保存・auditを照合した。ClaudeはBashの当該commandだけをallowedToolsに指定し、sandbox無効化なしをtool inputでも確認した。

| 観測 | Codex | Claude |
| --- | --- | --- |
| 認証済み応答・固定probe 1回・tool exit 0・launcher exit 0 | PASS | PASS |
| create stdoutはpathのみ、stderr空、host文書mode 0600、7 section本文一致、Session未記録 | PASS | PASS |
| 直接create／overwrite／rename／delete拒否、既存fixtureのbyte不変 | PASS | PASS |
| 他project mount不在、別project ID拒否、追加fileなし | PASS | PASS |
| malformed本文／合成credential marker拒否、final/temp増加なし | PASS | PASS |
| client・agent stdout/stderrとauditに本文sentinel・title・capability・dummy markerなし | PASS | PASS |
| runtime終了後run directory／socket／capability／project container不在、stale client拒否、audit不変 | PASS | PASS |

各runのauditはcreate成功（write）1件、別project拒否（authentication）1件、content-policy拒否2件。固定metadataと成功pathだけを持つことを検査した。Codex run labelは `1a5bbf6e036fe468`、Claudeは `9a73b60e3434c16f`。host保存文書はそれぞれ `2026-09-07_110920_6f49e593.md`、`2026-09-07_110956_ed552367.md`。

検証driverの初回はpreflight出力のcapture漏れでrootless結果をlauncherへ返せず、agent起動前にexit 1。さらに集計側がJSON scalarをeventと扱ってTypeErrorを出した。例外型と発生行による診断で特定し、driverのみを修正した。これらの試行ではhandover作成なし。上表は修正後の実行結果であり、製品コードの修正や境界緩和による再試行ではない。

host内の再確認用artifact:

- `/tmp/issue120-host-smoke.py`、`/tmp/issue120-probe.py`、`/tmp/issue120-prepare.py`
- `/tmp/issue120-host-build.log`、`/tmp/issue120-host-project-add.log`、`/tmp/issue120-host-doctor.log`
- `/tmp/issue120-host-codex.json`、`/tmp/issue120-host-claude.json`（固定metadataと判定結果のみ。生のagent streamは保存していない）

実装変更がないため、成功済みの全unit／CI Podman suiteはこのhost作業で再実行していない（not run：対象headのrequired CI成功を確認済み）。対話TUI操作はnot run（今回の6 gateは認証済み非対話CLIのtool実行で確認）。新規project／image／合成fixtureは再確認用に保持し、通常終了時のruntime cleanupとは区別する。

### S2-4の後片付け確認

同日にhost／GitHubで再照合した。PR #122はMERGED、merge commit `74fc0d7`、必須CI両方SUCCESS。host mainは同commitでclean、旧S2-4 local branch／worktreeはなく、`git ls-remote --heads origin docs/broker-kernel-s2-4`は空だった。

smoke PR `jj1xgo/agent-container-smoke#5` とIssue `jj1xgo/agent-container#121` はOPEN。`/tmp/s2-4-github-push-fixture` と `/tmp/s2-4-github-negative-fixture` のlocal Git fixture、`agent-container-smoke`のegress policy、`findsummits`のFamily bindingを保持している。Family pendingは `8c9c29e41edc7db563e9f09c59ef08e8` の1件（未送信）、期限は2026-09-08 04:12:21 UTC（13:12:21 JST）で、確認時点では未失効。送信／reject／外部close／削除／Appやbinding変更は行っていない。S2-4自体の完了と、保持したfixtureの後片付けを区別する。
