# Codex handover broker統一の検証記録

対象: [Issue #120](https://github.com/jj1xgo/agent-container/issues/120)。基準mainは `74fc0d7`（PR #122）。[設計](superpowers/specs/2026-09-07-codex-handover-broker-design.md)の申告session ID案を2026-09-07に利用者が承認した。Phase 6の保守であり、Phase 7は未着手。

## 現在の判定

実装とローカル検証は完了、remote CIとブランチ全体reviewを確認中。両agentの実host gateはnot runであり、Issue完了とは判定しない。今回のworkspaceにはPodman commandがなく、hostの認証済みCLIへ接続する実行手段も確認できていない。既存handoversに記録された実host結果を今回の結果へ読み替えない。

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
| required CI | 今回のPR | not run（PR作成前） |
| 実Podman | 今回のimage | not run（workspaceにPodmanなし） |
| 認証済み両agentの実host gate | 今回のimage/profile | not run（host実行手段・個別gate承認が未確認） |

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

| 元手順 | Codex経路の確認 | Claude回帰 | 今回 |
| --- | --- | --- | --- |
| 1 create | 完成本文、pathのみ、mode 0600、canonical未記録と本文の申告ID | 既存文書契約と7 section一致 | not run |
| 2 直接変更拒否 | create/write/overwrite/rename/delete拒否、fixtureのhash不変 | 同じ拒否と不変 | not run |
| 3 project境界 | 他project mount不在、別ID request拒否 | 同じ拒否 | not run |
| 4 content policy | malformed／安全なdummy marker拒否、final/temp増加なし | 同じ拒否 | not run |
| 5 non-logging | stdout/stderr/auditへ本文・title・capabilityを出さない | 同じ非露出 | not run |
| 6 終了後失効 | socket/capability/run directory不在、stale拒否、audit不変 | 同じcleanup | not run |

通常Codex tool sandbox内からのstdin送信も確認する。sandbox外commandの成功だけを通常tool sandboxの成功としない。起動／停止／再試行のunit・socket結果と、認証済みTUI結果も区別する。
