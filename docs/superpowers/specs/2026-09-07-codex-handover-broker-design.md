# Codex handoverのcreate-only broker統一 — 設計案

状態: 2026-09-07、利用者が推奨案（申告IDを本文に明示して保持）を承認。実装中。

調査基準: main `74fc0d7a308b7038ac6513afd979270306ae8569`（PR #122 merge）。
対象: [Issue #120](https://github.com/jj1xgo/agent-container/issues/120)。Phase 6の保守作業としてPhase 7より先に実施する。

## 問題と目的

Codexはhandover directoryをread-write mountし、wrapperで空文書を作ってから直接編集する。Claudeは同じdirectoryをread-only mountし、完成本文をhostのcreate-only brokerへ送る。Codexの通常tool sandboxで発生したread-only errorがhost側の保存不能と誤認されたことを契機に、両経路を後者へ統一する。

両agentとも `agent-handover create --title TITLE` のstdinへ完成した7 sectionを渡し、返されたpathを読み直す。既存文書のwrite、overwrite、rename、deleteはPodmanのread-only mountで拒否する。brokerが使用できない場合も直接書き込みへ切り替えない。

## 確認した実装と制約

- `src/agent_container/agentctl.py`: `run`のExitStackではhandover runtimeをClaudeの場合だけ開始する。Family、egress、GitHub、handoverの順に開始し、逆順に終了する。
- `src/agent_container/podman.py`: `run_codex_spec`はproject handoverとCodex homeをread-write mountする。`run_claude_spec`はhandover mountを必須引数で受け、read-onlyにする。
- `container/bin/agent-handover`: broker環境がない場合はdirect writerへ進み、`CODEX_SESSION_ID`をmetadataへ渡す。
- `src/agent_container/handover_broker_protocol.py`: version 1のrequestは6 fieldの完全一致。session fieldはない。
- `src/agent_container/handover_broker_transport.py`: host writerへsession IDを渡さず、canonical `Session`は `（未記録）` となる。
- `src/agent_container/handover_hook.py`: SessionStartでは最新pathの通知だけを行い、sessionをhostへ登録しない。
- `src/agent_container/profile.py`: profile versionは4。`update_codex_handover_profile`はhandover skill、専用commandのallow rule、sandbox network設定を更新する。

この構成では環境変数、hook payload、書き込み可能なCodex homeのsession fileはいずれもagent側で変更できる。UUID形式やfileの存在を検査しても「この会話の真正なID」である証明にはならない。今回確認したlauncherには信頼できる会話ID登録経路がない。Codex上流でそのような経路を構築可能かどうかは未調査であり、不可能とは断定しない。

## Sessionの選択肢と推奨

1. **申告IDを本文に明示して保持する（推奨）**。canonical `Session`は `（未記録）`。Codex skillは設定済みの`CODEX_SESSION_ID`を「現在地」sectionへ `Codex session ID（agent申告・host未検証）: …` と記録する。未設定ならその旨を書く。保存後にその行と値を読み直す。request schema、Claudeの文書形式、audit goldenを変更せず、会話再開用の手掛かりを残せる。ただし従来のcanonical `Session`欄から本文への移動は明示的な互換性変更であり、利用者の判断を要する。
2. **申告ID専用のprotocol fieldを追加する**。hostが形式・長さを検査し、未検証であることを示す別metadataへ出力する。機械的な抽出には適するが、真正性は向上せず、version互換性と旧imageの扱いまで変更範囲が広がる。
3. **hostが検証できる会話ID登録経路を先に実証する**。Codexの起動・resume・複数会話とtool processの関係を信頼できる経路で結び付ける別の調査が必要。書き込み可能なsession fileの照合を代用にしない。この方針なら実証結果が出るまで移行実装へ進まない。

以下は選択肢1を前提とする案。host生成のbroker run IDをCodex会話IDと呼ぶことや、container申告値を黙ってcanonical metadataへ昇格させることはしない。本文全体はagent申告の記録であり、認証や権限判定には使わない。session IDの保持はskill手順とその評価で検証するため、任意のclientによる本文からの省略をhostが防ぐ保証はない。

## Runtimeとcommand

`agentctl run`でCodexにも既存`HandoverBrokerRuntime`を開始する。Codex command builderに必須のhandover mountを渡し、欠落時の任意引数やdirect fallbackを設けない。既存のGitHub、egress、Familyとの組合せを維持する。

新規setupの保存先既定は`${state_root%/}-handovers`（通常は`~/.local/share/agent-container-handovers`）とする。従来のstate root配下という既定は新しい境界検査に通らないため、初回から分離する。既存登録と文書の自動移動は行わない。

handover project mountをread-onlyにし、既存socketとcapabilityの狭いread-only mountを追加する。project directoryと他のmount元の重複を防ぐClaude側の事前検査をCodexにも適用し、別のread-write mountから同じ保存先へ到達できないことを確認する。Codex homeや認証、workspaceの一般的な権限変更は目的に含めない。

containerの専用wrapperからdirect writer分岐を除く。socket／capabilityの欠落、一方だけの設定、接続失敗では非zeroで終了する。host用CLIとhost用skillは別経路として保持する。内部direct writer moduleの既存host用途を確認し、不要な削除は行わない。

起動順序、停止順序、publication guard、停止再試行、fd／thread回収は既存runtimeの契約に乗せる。kernelの一般化や監視方式の変更を混ぜない。clientへの固定error、本文・titleを含めないaudit、atomic createとmode 0600を維持する。

## 配布と移行

Codex配布skillを完成本文のstdin送信と返却pathの再読へ変更する。7 heading、credential除外、現状との照合は維持する。保存先がread-onlyでも読めることと、新規作成がbrokerで行われることを明記する。

profile versionを5へ進め、既存`project update-profile PROJECT`で配布する。個人のmodel、認証、hook trust、無関係なrulesは保持する。既存の限定allow ruleはsandbox外での専用command実行にも使用されるため、一般的なshell／Python許可へ拡大せず保持する。

更新前に対象fileと祖先directoryのsymlink・種別を検査する。skill更新失敗後にversionだけが5になることを防ぐ。旧profileの起動は更新案内付きでfail closedにし、旧skillで新wrapperを使う混在を防ぐ。対応imageのclient検査もCodex preflight／doctorへ接続する。自己検査が保証する範囲を説明し、broker到達性の実証と混同しない。

運用順序は、実行中sessionを終了し、対応imageをbuildし、対象projectのprofileを更新し、doctorを確認して新しいrunを開始する。更新の失敗を理由にread-write mountへ戻さない。旧imageへ戻す場合も旧direct経路へ自動移行せず、operatorによる別判断とする。

移行前の既存direct経路については、tool sandbox、Podman mount、host directoryの制約を分けて調べる。既存承認とpolicyが許す専用commandのsandbox外実行、実際の本文保存の確認を案内する。移行後はbroker不通をdirect writerや別保存先で回避しない。

## 変更対象

- 実装: `agentctl.py`、`podman.py`、`profile.py`、container wrapper。
- 配布: `profiles/codex/skills/handover/SKILL.md`。
- 運用: `docs/codex-operations.md`、README内の該当説明、roadmap、CHANGELOG、今回の検証記録。
- 検証: runtime／mount／wrapper／profile／doctorの既存testと、Codex skill評価。

既存smoke手順書とaudit goldenの期待値を今回の実装に合わせて緩めない。新しいCodex経路の手順対応と追加証拠は独立した検証記録へ置く。Phase 6完了の過去記録は保持し、roadmapでは現在の作業をIssue #120、次のPhaseを未着手のPhase 7と区別する。

## 検証と完了条件

1. mount specでCodex／Claudeのhandover read-only、対象project限定、broker mount必須、GitHub／egress／Familyとの組合せを検証する。
2. wrapperで完成本文の送信、環境欠落とbroker error時の非zero、空文書・direct fallback不在を検証する。
3. lifecycleで起動失敗、正常終了、停止失敗と再試行、listener／thread／fd／capability／audit／artifactの扱いを確認する。
4. session IDの設定済み・未設定・偽装申告がcanonical metadataを変更しないことを確認する。skill評価では未検証表記、保存後のID照合、通常sandboxのread-only errorをhost障害と誤認しないケースを扱う。
5. profile更新の旧version、再実行、個人設定保持、symlink、途中失敗、旧profile起動拒否を確認する。
6. Codex／container unit、関連socket integration、lint、差分検査を実行し、required CIのUnit testsとPodman integrationを通す。
7. 両agentの実host handover smokeでcreate、直接write／overwrite／rename／delete拒否、他project拒否、不正request、credential pattern、audit非露出、終了後staleとcleanupを確認する。過去のClaude PASSを今回のPASSとして再利用しない。

実host gateは実hostの接続・承認範囲を確認して実施する。このworkspaceではPodman commandが見つからず、実host gateを実行できることは確認できていない。未実施をPASSに読み替えず、Issue完了とmain mergeの判断へ明記する。

## 今回の調査・検証記録

- GitHub Issue #120本文とorigin/mainを直接照合し、引き継ぎのmain commitと一致した。
- `/workspace`の既存CI差分と未追跡handoverは保持。専用worktreeは `/workspace/.worktrees/codex-handover-broker`、branchは `feat/codex-handover-broker`。
- 基準commitでCodex unit 49件、container unit 1169件PASS。実装後の回帰確認ではない。
- runtime／profile／skillの実装変更、required CI、実Podman、両agentの実host smokeはnot run（設計段階）。
