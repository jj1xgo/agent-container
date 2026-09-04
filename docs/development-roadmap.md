# 開発ロードマップ

この文書は、`agent-container`の完了済みマイルストーンと、現在から順番に実施する開発Phaseを一か所で管理する。Phase番号は実施順を表し、同時に複数の番号体系を使わない。完了判定は、`main`へmergeされたcode、test、smoke記録、releaseを根拠とする。

## 現在地

Phase 1〜5は完了した。現在地は**Phase 6**である。Phase 5は、2026-09-02の実host smokeで専用App／binding、実PodmanのCodex／Claude両path、Codex intake、承認付き実Issue、non-exposure、duplicate、audit／cleanupがPASSし、残っていたClaude実CLIのintakeを2026-09-04に再実行してPASSしたことで閉じた。以前HTTP 401で停止していた原因は、browserのlogin codeをsetup tokenとして保存していた貼り間違いであり、validatorの強化（PR #79）と折り返しpasteの連結（PR #81）で再発を防いだ。

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
| Phase 6 | 共通control plane | 進行中 | project、agent、task、eventのhost側contract、private state、監査境界を固定する。 |
| Phase 7 | Obsidian Vault config sync | 未着手 | review可能なschemaをVault原本から検査済みprivate stateへ同期し、credential、session、cacheを除外する。 |
| Phase 8 | Worktree・task lease | 未着手 | agent別worktree、exclusive claim、期限、回収、stale writer拒否を提供する。 |
| Phase 9 | Conversation room | 未着手 | Codex／Claudeがtask単位のroomへ対等なparticipantとしてbounded read/postできる。 |
| Phase 10 | Obsidian UI | 未着手 | task、decision、room、feedback preview／approvalをVault上の人間向けprojectionから扱える。 |
| Phase 11 | Managed automation・相互review | 未着手 | review済みhook eventと、実装者／reviewerを分離したreview lifecycleをcontrol plane上で運用できる。 |
| Phase 12 | 外部integration | 未着手 | egress allowlist上で必要なHTTP MCPだけを追加し、必要性が確認されたserviceのcredential broker共通部を抽出する。 |
| Phase 13 | stdio MCP | 条件付き | subprocess credential隔離とClaude sandboxの安全な共存を実証してから、限定したstdio MCPを設計・提供する。 |

Phase 7〜10が「Obsidianを第2の脳として使う」ための中心範囲である。単なるVault全体mount、symlink、agentからの任意Markdown書き込みを代替案にしない。

## 現在の実施順

実施順はPhase番号と一致する。

1. Phase 6で、後続機能が共有するcontrol-plane contractを固定する。
2. Phase 7で、安全なVault原本と実行用copyの同期を作る。
3. Phase 8で、複数agentの編集をworktreeとleaseで隔離する。
4. Phase 9で、taskに紐づくconversation roomを作る。
5. Phase 10で、control planeの人間向けObsidian UIを作る。
6. Phase 11で、managed hooksと相互reviewを自動化する。
7. Phase 12で、必要なHTTP MCPとcredential brokerの共通化を行う。
8. Phase 13は、安全性の前提を実証できた場合だけ着手する。

Phase内の実装は複数の設計・PRへ分割できるが、完了条件を満たすまでPhase番号を進めない。緊急のsecurity fixや回帰修正はPhase外の保守作業として優先できるが、それだけで現在地を変更しない。

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
