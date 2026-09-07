# Phase 6 stage 2（S2-4）検証記録

2026-09-07、対象実装はmain `36f02a8`（PR #119 merge）。S2-1〜S2-3が取り込まれていることをlocal Git履歴で確認した。S2-4の今回の変更は文書のみで、Phase 6は進行中。stage 1の結果は[CHANGELOG](../CHANGELOG.md)の当時の記録を参照し、今回のPASSへ転用しない。

## 実行環境

- `/workspace`のsandbox。`command -v podman`はpathを返さず、実host用の実行toolもない。
- 初回の作業branch作成は通常tool sandbox内で`cannot lock ref ... Read-only file system`となった。その後、利用者のcommit依頼を受け、`require_escalated`で`docs/broker-kernel-s2-4`の作成に成功した。container／hostのGit metadata自体がread-onlyという意味ではない。PR作成はnot run。
- 開始時から`.github/workflows/ci.yml`に差分がある。HEADはPodman 17件だが、mountされたworking treeは2 moduleを欠き14件を期待する。既存差分は変更していない。
- required CIの新規実行・remote結果確認はnot run。この記録はlocal検証と実host検証を区別する。

## ローカル検証

結果は下表に実測で記録する。unit／socketの成功は実Podmanや認証済みCLIの成功を意味しない。

| 検証 | 実測結果 |
| --- | --- |
| `bin/lint` | PASS（All checks passed） |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex` | PASS、49件、skip 0 |
| 同じ環境変数で`python3 -m unittest discover -s tests/container` | FAIL、1169件中1件失敗、skip 0。`test_ci_requires_complete_family_podman_gate_without_skips`がworking treeのCIに`test_agent_sandbox_podman`が無いことを検出 |
| `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1`と上記環境変数で`python3 -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket tests.integration.test_family_intake_socket tests.integration.test_family_forced_unknown` | PASS、socket 18件＋forced unknown 4件＝22件、skip 0 |
| HEADの一時コピー＋今回の文書で`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_docs` | PASS、67件、skip 0。HEADのCI定義では上記失敗なし |

一時コピーは`git archive HEAD`を`/tmp/s2-4-verify`へ展開し、今回の文書をコピーして検証した。初回はGit metadataのないarchiveのため`git ls-files`を使う1件がERRORになり、一時コピー内だけで`git init`と文書の`git add`を行って再実行した。元workspaceのGit metadataとCIファイルには書き込んでいない。この67件の成功をcontainer全suite成功やrequired CI成功へ読み替えない。

文書検査: `git diff --check`成功。変更・新規Markdown 6件の相対linkと空白を確認し、CHANGELOGの既存の相対link誤り1件（Claude sandbox設計への`docs/`欠落）を修正した。既存smoke手順書5件はHEADとbyte一致。

## 実host smoke（すべてnot run）

理由: このsandboxにはPodmanと実host実行手段がない。認証やfixtureの現在状態も未確認。以下の既存手順書は変更していない。

| 手順書 | 再実行するgate | 今回の結果 |
| --- | --- | --- |
| [Phase 2](phase2-smoke-test.md) | 現行方式のClaude sandbox、認証、編集・test・resume、non-exposure、handover・cleanup（historical diagnosisは再実行対象外） | not run |
| [Phase 3](phase3-github-broker-smoke-test.md) | inventory／doctor、non-exposure、clone／fetch、create-only push、PR、Issue read、audit／cleanup | not run |
| [Phase 4](phase4-stabilization-smoke-test.md) | 現行private fixture gate、create-only拒否、Issue read、stale client、cleanup、自動検証・review（過去のrelease公開操作は再実行しない） | not run |
| [Egress](egress-domain-allowlist-smoke-test.md) | 手順書のallowlist／denial、実Codex／Claude、audit、cleanup／rollback | not run |
| [Family](family-issue-create-broker-smoke-test.md) | 実Podman 17件、App／binding、両CLI intake、non-exposure、duplicate、承認付きIssue、unknown／reconcile、audit／cleanup／rollback | not run |

GitHubはSameUserによる実clientの許可とjoin後失効を確認し、30秒client timeoutが実操作へ与える影響を記録する。egressは現在のcapability mode `0600`でadapterが起動することも既存手順中で確認する。

## 実hostでの続行

1. 対象checkoutのcommitと差分を再確認し、required CIのunit・socket 3 module・Podman 17件を照合する。現在のworking treeの古いCIを基準にしない。
2. 各手順書を読み、rootless Podman／crun、認証済みCLI、専用project、fixture、App／bindingを確認する。build対象commit、image ID、CLI version、実施日時を記録する。
3. 上表の現行gateを既存command・assertionのまま実行する。GitHub mutation、Family実Issue作成、rollbackは各手順書の対象と承認条件を満たしてから実施する。秘密値や本文は記録しない。
4. gateごとにPASS／FAIL／not runと理由・bounded evidenceを追記する。cleanupとrollbackを分け、未実施のrollbackを成功扱いしない。再実行に必要な承認は具体的な対象が揃ってから受ける。
5. 必要な実host gateとCIがPASSし、文書・証拠がmainへ取り込まれた後にPhase 6を閉じる。未実施項目が残る間は進行中を維持する。

## 次の実装

利用者指定（2026-09-07）: S2-4の実host smoke・required CI・必要なmain取り込みを完了した直後、Phase 7へ進む前に[Issue #120: Codex handoverのcreate-only broker統一](https://github.com/jj1xgo/agent-container/issues/120)へ最優先で着手する。S2-4へruntime変更を混ぜず、独立した設計・実装PRとする。
