# Codex handover broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Codexも完成本文をcreate-only brokerへ送信し、保存済みhandoverを直接変更できなくする。

**Architecture:** 既存handover runtimeとwire/auditを再利用する。Codex session IDは本文の未検証申告として保持する。profile version 5で移行を明示する。

**Tech Stack:** Python標準library、POSIX shell、rootless Podman、unittest。

**Spec:** docs/superpowers/specs/2026-09-07-codex-handover-broker-design.md

## Global Constraints

- 基準mainは74fc0d7。作業はfeat/codex-handover-brokerの専用worktree内。
- session案1は利用者承認済み。canonical Sessionは（未記録）、申告IDは本文。
- wire version 1、既存audit golden、既存smoke手順書、host writerを変更しない。
- broker失敗時にdirect writer/read-write mountへfallbackしない。
- 個人設定、credential、他worktreeの既存差分を保持する。
- 実Podmanと認証済み実hostの結果はunitや過去のPASSで代用しない。

### Task 1: Runtime・wrapper・profileの移行

**Files:** `src/agent_container/agentctl.py`, `src/agent_container/podman.py`, `src/agent_container/profile.py`, `container/bin/agent-handover`; 対応する `tests/container/test_agentctl.py`, `test_podman.py`, `test_profile.py`, `test_agent_handover_wrapper.py` および同APIを使うtest。

**Interfaces:** `run_codex_spec(layout, handover_project, image, uid, gid, handover_broker, broker=None, egress=None, family_mount=None)` とする。handover_brokerは必須 `HandoverRuntimeMount`。既存 `HandoverBrokerRuntime.create(layout, handover_project)` を両agentで利用する。profile.pyへ `validate_codex_handover_profile(codex_home: Path) -> None` を追加し、run preflight/doctorから呼ぶ。

- [x] RED: wrapperの既存direct成功testを、broker環境欠落で非zero・stdout空・project directory空という回帰testへ置換する。片方だけ設定、接続失敗、CODEX_SESSION_IDありでも同条件を検証する。

```python
self.assertNotEqual(completed.returncode, 0)
self.assertEqual(completed.stdout, '')
self.assertEqual(list((handover_root / 'project').iterdir()), [])
```

- [x] RED: Codex mountはread-only、socket/capability必須。既存の両agent lifecycle testのClaude限定期待を両者へ拡張する。Codexの重複mount拒否、起動失敗、停止失敗とcleanup再試行は実際のproduction経路で確認する。

```python
self.assertIn('type=bind,src=' + str(handover_project) + ',dst=/handovers/agent-container,ro=true', spec.argv)
```

mount文字列の具体的なreadonly表現は既存 `_mount` が返す表現に合わせる（安全性の期待はread-only固定）。関連unitを実行し、仕様差分による失敗を記録する。

- [x] GREEN: wrapperは引数形を保ちbroker clientだけをexecする。`run_codex_spec`へ必須handover mountを追加。共通の事前mount境界検査を両agentで呼ぶ。agentctlのExitStackへ両agentのhandoverを接続し、既存起動／停止順序を維持する。

```python
handover_mount = stack.enter_context(
    HandoverBrokerRuntime.create(layout, handover_project)
)
```

- [x] RED: profile旧version拒否とupdate案内、version5正常、祖先symlink拒否、個人設定保持、再実行、更新途中失敗でversionが進まないtestを追加する。
- [x] GREEN: PROFILE_VERSIONを5へ進める。更新前に全対象と祖先の型／symlinkを検査し、versionを最後に書く。旧profileをrun前に拒否。Codex doctorにもprofileとclient probeを追加し、probe出力を露出しない。runでclient unavailableならbroker/Podman runtimeを開始しない。
- [x] 対象unit、container suite、lintを実行する。古いrun_codex_spec caller/fixtureを新必須mountとprofile versionへ追従させる。controllerが変更するdoc/skill contractの失敗は詳細を報告し、勝手に文書期待を緩めない。
- [x] 自己reviewし、担当code/testだけcommitする。報告にRED/GREENコマンド、結果、差分、not runを残す。

### Task 2: 配布skill・運用・検証記録

**Files:** `profiles/codex/skills/handover/SKILL.md`, `docs/codex-operations.md`, `README.md`, `docs/development-roadmap.md`, `CHANGELOG.md`, `docs/codex-handover-broker-validation.md`, 必要なら既存doc/skill contract test。

**Interfaces:** Task 1のversion5、broker専用wrapperを説明する。protocolの新fieldは作らない。

- [x] 旧skillでsandbox read-onlyケースとbroker stdinケースをfresh agentへ評価させ、観測と限界を記録する。
- [x] skillを次の構造へ改訂する。

```sh
agent-handover create --title "Codex作業引き継ぎ" < /tmp/agent-handover-BODY.md
```

本文fileは自分で作ったmode0600の一時fileで7 sectionのみ。現在地sectionに `Codex session ID（agent申告・host未検証）: VALUE` または未設定を記す。完成本文を一回送信し、返却pathの本文・Session未記録・申告ID・Git/test事実を再読検証する。broker失敗時は保存未完了。移行前のread-only errorでは許可された専用command経路を確認する。
- [x] 同じscenarioを新版skillで評価する。skillの参照先・既存doc contractを整合させる。単なる文字列testを振る舞い評価の代用にしない。
- [x] README/operationsにbuild→update-profile→doctor→新run、Session変更、権限層の切り分けを記載する。roadmapはIssue120進行中・Phase7未着手を示す。旧smoke文書は改変せず新検証記録へ対応付ける。
- [x] 関連文書検査とuntracked含むwhitespace検査を実行する。spec、plan、doc、skillをcommitする。

### Task 3: 統合検証・review・PR

**Files:** Task1/2の結果、検証記録とplan。

- [x] Codex/container全unit、bin/lint、CI指定のbroker socket suiteを実行。実Podmanが使えなければnot runと理由を記録する。
- [x] 独立reviewでcredential/mount境界、起動・停止・再試行、profile移行、Session、fallback禁止、テスト妥当性を確認し、指摘を修正・対象再検証する。
- [x] [draft PR #123](https://github.com/jj1xgo/agent-container/pull/123)を作成。required CIの最新headの最終判定はPR checksを参照する。main mergeや実サービス操作の承認は推測しない。
- [x] 両agent実host smokeの必要操作と実行可能環境を照合し、可能な範囲を実施する。環境外のgateはnot runとし、Issue完了とは報告しない。
