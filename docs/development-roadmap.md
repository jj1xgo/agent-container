# 開発ロードマップ

この文書は、`agent-container`の製品Phase 1〜4に続く開発項目について、当初計画、実績、順序変更の理由、現在の推奨順を一か所で管理する。完了判定は、未更新の計画checkboxではなく、`main`へmergeされたcode、test、smoke記録、releaseを根拠とする。

## 位置づけ

2026-08-27のhandoverでは、次期作業を暫定的にPhase 0〜13として整理した。しかし、既存の製品ロードマップがすでにPhase 1〜4を使用していたため、この文書では同じ項目をPhase 5〜18へ正規化する。旧番号は履歴上の別名であり、今後の設計、計画、PR、handoverではPhase 5〜18だけを使用する。

状態は次の4種類とする。

- `完了`: 必要な実装と自動検証が`main`へmerge済み。
- `一部完了`: 独立して利用できる成果はあるが、Phaseの完了条件を満たさない。
- `未着手`: Phase固有の成果物がまだない。
- `運用待ち`: 実装済みだが、credentialや外部状態を使う承認付きsmokeが残る。

## 当初計画と現在地

| 正式Phase | 2026-08-27の項目 | 状態 | 現在の根拠と残作業 |
| --- | --- | --- | --- |
| Phase 5 | baseline修正とrelease | 完了 | version検証を安定化し、`v0.2.0`をreleaseした。 |
| Phase 6 | Claude handover writerとGitHub Issue read | 完了 | create-only handover brokerは`v0.3.0`、Issue list/viewは`v0.4.0`で提供した。 |
| Phase 7 | 共通control plane | 一部完了 | `agentctl`、project profile、doctor、broker runtime、setupは共通化済み。task、lease、roomを束ねる永続的なcontrol-plane contractは未実装。 |
| Phase 8 | Vault config sync | 未着手 | Vaultを人間向け原本、private stateを検査済み実行用copyにする同期経路はない。Vault全体をruntimeへmountしない方針は維持する。 |
| Phase 9 | agent別worktreeとtask lease | 未着手 | agentごとのworktree所有権、task claim、期限、回収、競合拒否を管理する仕組みはない。 |
| Phase 10 | conversation room | 未着手 | Codex／Claudeが対等なparticipantとしてread/postするhost側roomとbounded adapterはない。 |
| Phase 11 | feedback inbox | 完了 | Family Issue brokerのpending request、重複・limit・secret検査、preview、approve/reject、unknown reconciliationとして実装した。正式な実host smokeは運用待ち。 |
| Phase 12 | Obsidian UI | 未着手 | pending feedback、task、decision、roomをVault上で閲覧・承認するprojectionはない。現状のObsidian利用はproject別handover保存に限定される。 |
| Phase 13 | Issue publish | 完了 | 開発Appと分離したFamily専用Appにより、hostのrequest単位承認後だけ登録repositoryへIssueを1件作成できる。正式な実Issue smokeは運用待ち。 |
| Phase 14 | managed hooks | 一部完了 | Codex handover通知など限定hookはある。review済みeventをcontrol planeへ安全に記録する共通managed hooksは未実装で、Claude hooksは初期無効を維持する。 |
| Phase 15 | network allowlistとHTTP MCP | 一部完了 | project-scoped exact-domain egress allowlistは実装済み。review済みHTTP MCPの配布・認証・policyは未実装。 |
| Phase 16 | credential broker一般化 | 一部完了 | GitHub、handover、egress、Familyは用途別brokerとして実装済み。任意serviceへ広げる共通credential brokerはなく、必要性が確定したserviceだけを対象にする。 |
| Phase 17 | 相互review | 一部完了 | 共通PR templateとcross-agent review手順はある。task lease、conversation room、独立worktreeを使う自動的な相互review lifecycleはない。 |
| Phase 18 | stdio MCP | 未着手 | subprocessへのcredential伝播を安全に隔離できないため、Claudeでは引き続き無効とする。 |

## 当初順序から変更した理由

順序変更は単一の再計画で決めたものではなく、次の要因が重なった結果である。

1. **roadmapがtracked artifactではなかった。** 当初順はhandoverに「概ね」として保存され、repository内の設計、Issue、milestoneには昇格されなかった。そのため、各セッションは最新handoverの局所的な次の一手を優先した。
2. **Phase番号が衝突した。** 次期計画のPhase 0〜13と、release／smokeで使用中の製品Phase 1〜4が併存し、後者が実際の進捗管理に使われた。
3. **安全上のblockerが実host gateで判明した。** Git receive-pack framing、private repositoryのruleset制限、既存branchへのforce-push受理、Family runtimeのPID登録前実行などは、後続機能より先に閉じる必要があった。
4. **依存関係が具体化した。** HTTP MCPより先にegress制御が必要であり、Obsidian UIより先にhost側のfeedback state machineとrequest単位承認が必要だと分かった。
5. **直近の利用価値が高い項目を前倒しした。** domain allowlistとFamily feedback inbox／Issue publishは、既存runtimeを安全に実運用へ近づけるため先行した。
6. **順序変更を戻す更新規則がなかった。** 設計、handover、release記録は残ったが、全体roadmapを更新するownerと完了条件が定義されていなかった。

このため、Phase 11とPhase 13は、前提となる安全境界を個別に実装する形でPhase 8〜10より先に提供された。これはObsidian UIを完成したことを意味しない。

## 現在の推奨実装順

完了済みPhaseを作り直さず、残る依存関係を次の順で閉じる。

1. **Family実host smoke（運用gate）**: 専用App、binding、Codex／Claude intake、duplicate／non-exposure、承認付き実Issue、unknown reconciliation、cleanupを記録する。
2. **Phase 7 共通control plane完成**: project、agent、task、eventのhost側contractと、private state／監査境界を固定する。
3. **Phase 8 Vault config sync**: review可能なschemaだけをVault原本から検査・反映し、credential、session、cacheを除外する。runtimeへVault全体をmountしない。
4. **Phase 9 worktree／task lease**: agent別worktree、exclusive claim、期限、回収、stale writer拒否を実装する。
5. **Phase 10 conversation room**: taskに紐づくbounded message、participant identity、read/post capability、content-free auditを実装する。
6. **Phase 12 Obsidian UI**: control planeの状態をVaultへ安全にprojectionし、task、decision、feedback preview／approvalへの人間向け導線を提供する。
7. **Phase 14 managed hooks**: review済みeventだけをcontrol planeへ送り、失敗時にagentの権限やmountを広げない。
8. **Phase 17 相互review完成**: worktree、lease、roomを使い、実装者とreviewerを分離する。
9. **Phase 15 HTTP MCP**: 完成済みegress allowlistの上で、必要なremote MCPだけをallowlist、認証、response制限付きで追加する。
10. **Phase 16 credential broker一般化**: 実際に必要となったserviceについてのみ、operation固有brokerから再利用可能な最小共通部を抽出する。
11. **Phase 18 stdio MCP**: subprocess credential隔離を実証できた場合だけ設計する。

Phase 8〜10は「Obsidianを第2の脳として使う」ための基盤、Phase 12はその人間向けUIである。単なるVault全体mount、symlink、agentからの任意Markdown書き込みを代替案にしない。

## 更新規則

- Phaseを前倒し、延期、分割、置換するときは、同じPRでこの文書を更新する。
- 状態を`完了`にするPRは、根拠となるtest、smoke、運用文書を明記する。
- external-state smokeが未実施なら、codeがmerge済みでも`運用待ち`を併記する。
- 新しいPhase番号を追加する前に、既存Phaseのscope拡張で足りない理由を記録する。
- handoverは現在作業の継続記録に使い、roadmapの唯一の原本にはしない。
- credential、token、capability、private key、session、cacheをVaultへ保存しない。

## 履歴資料

- 2026-08-27 handover `Phase 0開始前・複数AI開発基盤ロードマップ合意`
- `CHANGELOG.md`の`v0.2.0`〜`v0.4.0`および`Unreleased`
- `docs/phase4-stabilization-smoke-test.md`
- `docs/egress-domain-allowlist.md`
- `docs/family-issue-create-broker.md`
- `docs/family-issue-create-broker-smoke-test.md`
