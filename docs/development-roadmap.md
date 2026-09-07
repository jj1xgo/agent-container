# 開発ロードマップ

この文書は、`agent-container`の完了済みマイルストーンと、現在から順番に実施する開発Phaseを一か所で管理する。Phase番号は実施順を表し、同時に複数の番号体系を使わない。完了判定は、`main`へmergeされたcode、test、smoke記録、releaseを根拠とする。

## 現在地

Phase 5の成果は専用`release/0.5`系統の`v0.5.0`として切り出す。Phase 6の保守はmainの`0.6.0-dev`で継続する。公開候補と検証状態は[release記録](v0.5.0-release.md)を参照する。

Phase 6完了時のrelease判断が未記録だったため、2026-09-07に`v0.6.0`の準備を開始した。公開済みstableは引き続き`v0.5.0`。候補の検証・移行上の注意は[v0.6.0 release記録](v0.6.0-release.md)にまとめる。公開判断は最終候補のrequired CI完了後、Phase 7実装前に行う。機能完了とrelease公開は区別する。

Phase 1〜6は完了した。次のPhaseは**Phase 7**（未着手）である。利用者指定で最優先としたPhase 6の保守作業、[Issue #120](https://github.com/jj1xgo/agent-container/issues/120)（Codex handoverのcreate-only broker統一）も、2026-09-07に[PR #123](https://github.com/jj1xgo/agent-container/pull/123)をmain `b17e179`へ取り込み、Issueを完了として閉じた。Phase 6は、stage 1（PR #106まで）とstage 2（S2-1〜S2-3、PR #117〜#119）を取り込んだmain `36f02a8`に対し、2026-09-07の実host smoke（[検証記録](broker-kernel-stage2-validation.md)の現在地一覧）で、stage 2が観測挙動を変えた4 broker（handover、egress、GitHub、Family）の既存手順gateがすべてPASSし、その記録をS2-4（PR #122）で取り込んだことで閉じた。stage 2対象外の一部項目はstage 1証拠を採用し、各rollbackはnot run（policy／binding保持）のまま記録している。Phase 5は、2026-09-02の実host smokeで専用App／binding、実PodmanのCodex／Claude両path、Codex intake、承認付き実Issue、non-exposure、duplicate、audit／cleanupがPASSし、残っていたClaude実CLIのintakeを2026-09-04に再実行してPASSしたことで閉じた。以前HTTP 401で停止していた原因は、browserのlogin codeをsetup tokenとして保存していた貼り間違いであり、validatorの強化（PR #79）と折り返しpasteの連結（PR #81）で再発を防いだ。

状態は次の4種類とする。

- `完了`: Phaseの完了条件を満たし、必要な実装と検証が`main`へmerge済み。
- `進行中`: 現在取り組むPhase。先行成果があっても完了条件を満たすまでは進めない。
- `未着手`: Phase固有の実装を開始していない。
- `条件付き`: 前提となる安全性や必要性を実証できた場合だけ着手する。

## 全体Phase一覧

| Phase | 内容 | 状態 | 完了条件 |
| --- | --- | --- | --- |
| Phase 1 | Codex隔離runtime | 完了 | rootless Podman、project別state、認証、handover、実host smokeが成立する。 |
| Phase 2 | Claude Code対応 | 完了 | managed sandbox、認証、編集・test・resume、credential非露出の実host gateが成立する。 |
| Phase 3 | GitHub App broker | 完了 | credentialをcontainerへ渡さず、exact repositoryのclone／fetch、create-only push、PR、Issue readを提供する。 |
| Phase 4 | scope整合・安全性安定化・`v0.4.0` | 完了 | private fixture gate、create-only強制、Issue read、cleanup、releaseを完了する。 |
| Phase 5 | Family機能の運用完成 | 完了 | 専用App、binding、Codex／Claude intake、non-exposure、duplicate、承認付き実Issue、unknown reconciliation、cleanupの実host smokeがPASSする。 |
| Phase 6 | 共通broker kernel | 完了 | GitHub／handover／egress／Familyの4 brokerが仕様に記録した共通kernel部品と互換adapter上で動き（stage 1）、残したlifecycle・capability・audit opener等を統一し、kernelがreadiness gate・fail-closed cleanup・統一auditを全brokerへ提供し（stage 2）、既存の実host smoke手順が変更なしでPASSする。設計は[`docs/superpowers/specs/2026-09-04-broker-kernel-design.md`](superpowers/specs/2026-09-04-broker-kernel-design.md)。 |
| Phase 7 | Obsidian Vault config sync | 未着手 | review可能なschemaをVault原本から検査済みprivate stateへ同期し、credential、session、cacheを除外する。 |
| Phase 8 | Worktree・task lease | 未着手 | task・event・agentのhost側contractをleaseの最初の消費者として定義し、agent別worktree、exclusive claim、期限、回収、stale writer拒否を提供する。 |
| Phase 9 | Conversation room | 未着手 | Codex／Claudeがtask単位のroomへ対等なparticipantとしてbounded read/postできる。 |
| Phase 10 | Obsidian UI | 未着手 | task、decision、room、feedback preview／approvalをVault上の人間向けprojectionから扱える。 |
| Phase 11 | Managed automation・相互review | 未着手 | review済みhook eventと、実装者／reviewerを分離したreview lifecycleをcontrol plane上で運用できる。 |
| Phase 12 | 外部integration | 未着手 | egress allowlist上で必要なHTTP MCPだけを追加し、必要性が確認されたserviceのcredential broker共通部を抽出する。 |
| Phase 13 | stdio MCP | 条件付き | subprocess credential隔離とClaude sandboxの安全な共存を実証してから、限定したstdio MCPを設計・提供する。 |

Phase 7〜10が「Obsidianを第2の脳として使う」ための中心範囲である。単なるVault全体mount、symlink、agentからの任意Markdown書き込みを代替案にしない。

## 現在の実施順

Phase 6 stage 2のS2-1（[PR #117](https://github.com/jj1xgo/agent-container/pull/117)）、S2-2（[PR #118](https://github.com/jj1xgo/agent-container/pull/118)）、S2-3（[PR #119](https://github.com/jj1xgo/agent-container/pull/119)）はmainへ取り込み済みです。2026-09-07の照合基準は`36f02a8`です。S2-4（文書整合とstage 2実host smoke、[PR #122](https://github.com/jj1xgo/agent-container/pull/122)）で実host smokeの記録を取り込み、Phase 6を閉じました。

範囲は[stage 2設計](superpowers/specs/2026-09-06-broker-kernel-stage2-design.md)で固定しています。kernelのfail-closed lifecycle、identity付きcleanup、peer policy、frame error、audit envelopeを整備し、handover／egress／GitHubを統一しました。Familyはpeer policyとcleanupを共通部品へ移し、request毎のpeer検証、audit transaction、Mountを保持しています。readiness gateの新しい消費者と全brokerへの祖先chain検証は後続課題です。

stage 1（6-6）の実host観測と独立修正の経緯は[CHANGELOG](../CHANGELOG.md)に保存しています。Codex／Claude intakeやhandoverの過去のPASSを、stage 2の再実行結果として扱いません。S2-4の[実施計画](superpowers/plans/2026-09-07-broker-kernel-s2-4-validation.md)と[検証記録](broker-kernel-stage2-validation.md)で今回の結果と未実施理由を管理します。2026-09-07のhostで、専用image `e9791cbc483f`（Codex 0.153.4、Claude 2.1.263）を使い、自動gate（lint、unit、socket、実Podman 17件）と、Codex／Claude runtime、GitHub broker、Family、Claude handover createの実host gateが既存手順の変更なしでPASSしました。stage 2対象外で未再実施の項目はstage 1証拠を採用し、rollbackはnot runのまま記録しています。

利用者指定（2026-09-07）に従い、S2-4のmain取り込み後、Phase 7より先に[Issue #120: Codex handoverのcreate-only broker統一](https://github.com/jj1xgo/agent-container/issues/120)を独立した[PR #123](https://github.com/jj1xgo/agent-container/pull/123)で完了した。[設計](superpowers/specs/2026-09-07-codex-handover-broker-design.md)に従い、session IDはhost未検証の申告として本文に残す。認証済みCodex／Claudeの非対話CLIから通常sandboxのtoolを実行し、両agentの実host 6 gateがPASSした。対話TUIの操作確認とは区別し、詳細と未実施項目は[検証記録](codex-handover-broker-validation.md)を参照する。既存projectへの適用はこの完了判定に含めず、必要な場合は[既存projectの更新手順](codex-operations.md#既存projectの更新)に従って別途確認する。次の開発候補はPhase 7の設計であり、現時点では未着手。

実施順はPhase番号と一致します。

1. Phase 6で、後続機能が共有するbroker kernelを固定する。
2. Phase 7で、安全なVault原本と実行用copyの同期を作る。
3. Phase 8で、複数agentの編集をworktreeとleaseで隔離する。
4. Phase 9で、taskに紐づくconversation roomを作る。
5. Phase 10で、control planeの人間向けObsidian UIを作る。
6. Phase 11で、managed hooksと相互reviewを自動化する。
7. Phase 12で、必要なHTTP MCPとcredential brokerの共通化を行う。
8. Phase 13は、安全性の前提を実証できた場合だけ着手する。

Phase内の実装は複数の設計・PRへ分割できるが、完了条件を満たすまでPhase番号を進めない。緊急のsecurity fixや回帰修正はPhase外の保守作業として優先できるが、それだけで現在地を変更しない。

## Phase 6〜9の依存理由

Phase 6（共通control plane）→Phase 7（Vault config sync）→Phase 8（Worktree・task lease）→Phase 9（Conversation room）の順序は、単なる技術的な積み上げではなく、**CodexとClaudeを対等に協働させ、並行して作業を進められるようにする**という利用者の目的のために2026-08-27に合意された順序である。

- 出典: handover `2026-08-27_031151_4dc890f8`（Phase 0開始前・複数AI開発基盤ロードマップ合意）。
- 決定事項: 「CodexとClaudeを対等に協働させるには、agent別Git worktree、task lease、host側conversation roomを使う。MCPは相手をtool化するのでなく、両者が同じroom read/post capabilityを使う通信adapterとしてなら利用可能。」
- Phase 8（Worktree・task lease）とPhase 9（Conversation room）が、この協働を実際に可能にする機能である。Phase 6（共通control plane）を先に固定するのは、GitHub／handover／egress／Familyの各brokerがこれまで個別に重複実装してきた問題（「先行実装済みの部品」節参照）を、worktree/leaseやconversation roomでも繰り返さないため。
- 現在のworkspace実装は`root/workspaces/<project_id>`というproject単位の単一directoryであり（`src/agent_container/state.py`）、agentごとの分離やleaseはまだない。同一projectに対して複数agentを同時実行することは、Phase 8完了まで安全でない。
- この理由は2026-08-27時点で「概ね」の順序として合意されたものであり、絶対的な技術依存として再検証されたわけではない。
- 2026-09-04のPhase 6 brainstormingで次を決めた。Phase 8の一部（agent別worktreeの分離だけ）の前倒しは行わない。worktree分離はlease（誰がどこを書いてよいか）と不可分で、分離だけ入れても同一branchへの競合は防げないためである。また、Phase 6定義に含めていたtask・event・agentのhost側contractは、最初の消費者であるPhase 8のleaseと一緒に定義するためPhase 8へ移した。Phase 6は既存4 brokerの共通kernel化に集中する。

## 先行実装済みの部品

将来構想の一部は、安全性や直近の実用性を優先して先行実装された。これらは後続Phaseを完了した証拠ではなく、各Phaseで再利用する既存部品として扱う。

- Family feedback inboxと承認付きIssue publishはPhase 5へ統合する。
- `agentctl`、project profile、doctor、各broker runtimeはPhase 6の土台とする。
- project-scoped exact-domain egress allowlistはPhase 12の前提として完成済み。
- Codex handover通知hookと共通PR templateはPhase 11の土台とする。
- GitHub、handover、egress、Familyの用途別brokerはPhase 12で必要な範囲だけ共通化を検討する。

## 当初順序から変更した理由

2026-08-27のhandoverには、次期候補が暫定Phase 0〜13として「概ね」の順で保存されていた。その番号は既存の製品Phase 1〜4と衝突し、候補の一部が前倒しされた結果、実施順として意味を失った。旧Phase番号は現在の進捗管理に使用しない。

1. **roadmapがtracked artifactではなかった。** repository内の原本、更新owner、完了条件がなく、各セッションは最新handoverの局所的な次の一手を優先した。
2. **安全上のblockerが実host gateで判明した。** Git receive-pack framing、private repositoryのruleset制限、既存branchへのforce-push受理、Family runtimeのPID登録前実行は、後続機能より先に閉じる必要があった。
3. **依存関係が具体化した。** HTTP MCPより先にegress制御が必要であり、Obsidian UIより先にhost側feedback state machineとrequest単位承認が必要だと分かった。
4. **直近の利用価値を優先した。** domain allowlistとFamily feedback inbox／Issue publishを先行させ、既存runtimeを安全に実運用へ近づけた。
5. **順序変更をroadmapへ戻す規則がなかった。** 設計、handover、release記録は残ったが、全体の実施順は更新されなかった。

## 2026-08-27構想の保存

当時の候補は、baseline／release、Claude handoverとIssue read、共通control plane、Vault config sync、worktree／task、conversation room、feedback inbox、Obsidian UI、Issue publish、managed hooks、network allowlist／HTTP MCP、credential broker一般化、相互review、stdio MCPだった。

本roadmapでは、baseline、Claude handover、Issue readを完了済みPhase 1〜4の実績へ、feedback inboxとIssue publishをPhase 5へ統合した。control planeからObsidian UIをPhase 6〜10、managed hooksと相互reviewをPhase 11、network／HTTP MCPとcredential brokerをPhase 12、stdio MCPをPhase 13として依存順に再編した。

## 更新規則

- Phase完了時にはreleaseするか、延期するなら理由と次の判定時点を同じPRへ記録する。機能・実host検証の完了と、tag／GitHub Releaseの公開を分けて報告する。

- Phaseを分割、統合、延期、置換するときは、同じPRでこの文書を更新する。
- 現在Phaseを変更するPRは、直前Phaseの完了条件と検証証拠を明記する。
- external-state smokeが未実施なら、codeがmerge済みでもPhaseを`完了`にしない。
- security fixや回帰修正を前倒ししても、それだけで後続Phaseを完了扱いにしない。
- 新しいPhaseを追加する前に、既存Phaseへ含められない理由を記録する。
- handoverは現在作業の継続記録に使い、roadmapの唯一の原本にはしない。
- credential、token、capability、private key、session、cacheをVaultへ保存しない。

## 履歴資料

- 2026-08-27 handover `Phase 0開始前・複数AI開発基盤ロードマップ合意`
- `CHANGELOG.md`の`v0.1.0`〜`v0.4.0`および`Unreleased`
- `docs/phase4-stabilization-smoke-test.md`
- `docs/egress-domain-allowlist.md`
- `docs/family-issue-create-broker.md`
- `docs/family-issue-create-broker-smoke-test.md`
