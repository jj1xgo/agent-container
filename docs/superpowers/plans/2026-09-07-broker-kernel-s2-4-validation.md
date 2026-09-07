# Broker kernel S2-4 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 文書を実装に合わせ、stage 2実host smokeの証拠を記録してPhase 6の完了を判定する。

**Architecture:** S2-1〜S2-3の実装は変更せず、既存smoke手順書をそのまま実行する。過去のstage 1証拠と今回のstage 2証拠を区別する。

**Tech Stack:** Markdown、Python unittest、rootless Podman。

**Spec:** [stage 2設計](../specs/2026-09-06-broker-kernel-stage2-design.md)

## Constraints

- 基準commitは`36f02a8`（S2-3のPR #119 merge済み）。S2-2のPR #118も祖先に含む。
- wire、audit、runtime、smoke手順書は変更しない。
- 実host smokeがPASSするまでPhase 6を完了にしない。承認や前提不足は`not run`と記録する。
- `.github/workflows/ci.yml`の既存差分を保持する。通常tool sandbox内のGit書き込みはread-onlyで失敗したが、利用者のcommit依頼後に`require_escalated`で作業branch作成に成功した。権限エラーは実行層を区別する。

## Task 1: 文書整合

Files: stage 1／stage 2設計、`docs/development-roadmap.md`、`CHANGELOG.md`。

- [x] Git履歴と実装を確認し、古いhandoverの未merge記述を現在の事実として使わない。
- [x] stage 1のcleanup順序（capability → socket → run directory）が既に訂正済みであることを確認し、K2への参照を明示する。
- [x] stage 1のegress mode `0400`は歴史記録と明示し、現在の`0600`（E1／E2）へ参照する。
- [x] G1に`deactivate_after_join=True`と実装上の理由を追記する。
- [x] roadmapの現在地をS2-4へ更新し、stage 1の詳細証拠はCHANGELOGへ参照する。

## Task 2: 検証と実hostへの引き継ぎ

Files: `docs/broker-kernel-stage2-validation.md`、`CHANGELOG.md`。

- [x] `bin/lint`、Codex／container unit、socket 4 module、forced unknownを実行し、件数・失敗・skipを記録する。
- [x] ホストへ`5a2a49a`を復元し、専用imageをbuild。lint、Codex 49件、container 1169件、socket＋forced unknown 22件、実Podman 17件の成功と前提確認を検証記録へ追記する（認証済みCLI smokeは未実施）。
- [x] 既存smoke 5手順書を実hostで再実行し、commit、image、CLI version、実施日時、各gateの結果を記録する。実行順と前提は検証記録から各手順書を参照する。2026-09-07、検証記録冒頭の現在地一覧に対応付け済み。stage 2対象の4 brokerは実host PASS、stage 2対象外の未実施項目はstage 1証拠を採用（利用者判断）。
- [x] fresh approvalを要する外部変更は対象が具体化した直前に判断を受ける。以前のIssue／PRを再利用・変更せず、rollback未実施をPASSへ読み替えない。smoke PR #5、Issue #121、Claude handover createの各1回を個別承認で実施し、rollbackはnot runのまま記録。
- [x] required CI（unit、socket 3 module、Podman 17件）の対象commitと成功を確認する。PR #122の`34829c6`でUnit tests／Podman integrationともpass。
- [x] 全実host gateのPASSと必要なmergeを確認した後だけroadmapのPhase 6を完了へ変更する。S2-1〜S2-3のmergeと実host gateを確認し、PR #122でroadmapを完了へ変更（merge後に確定）。
- [x] `git diff --check`、相対link、smoke手順書が不変であることを確認する。

## S2-4完了直後の実装

利用者指定（2026-09-07）: S2-4の実host smoke・required CI・必要なmain取り込みを完了した直後、Phase 7へ進む前に[Issue #120: Codex handoverのcreate-only broker統一](https://github.com/jj1xgo/agent-container/issues/120)へ最優先で着手する。S2-4へruntime変更を混ぜず、独立した設計・実装PRとする。
