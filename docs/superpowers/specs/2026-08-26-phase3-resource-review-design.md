# Phase 3 resource監視・cross-agent review標準化設計

## 目的

Phase 3の残項目として、実行中のCodex／Claude containerをproject単位で識別してresource使用量を安全に観測し、PRでagent間reviewの実施状況と検証境界を一貫して記録する。

## resource監視

runtime containerへ次の固定labelを付ける。

- `io.agent-container.managed=true`
- `io.agent-container.project=PROJECT`
- `io.agent-container.agent=codex|claude`

`agentctl stats PROJECT`はrootless Podmanを確認後、上記managed/project labelに一致するrunning container IDを`podman ps`から取得する。0件は固定error、複数件は各containerを表示する。別projectやlabelなしcontainerを通常のdiscovery対象にしない。

labelは認証やagent-container所有の証明ではない。同じrootless Podman userはlabelを偽装できるためsame-user containerはtrust boundary内の協調processとして扱い、敵対的same-user環境ではstats snapshotの真正性を保証しない。statsは同じuserが既にPodmanから取得可能なresource fieldだけを狭く再表示する。

表示は`podman stats --no-stream`の固定formatに限定し、container ID、agent label、CPU percentage、memory usage、PID count、uptimeだけを出す。環境変数、mount、argv、process command、network I/Oは表示しない。Podmanのrootless statsはcgroups v2を前提とし、取得失敗はfallbackせず非zeroで返す。

interactive TUIへ監視出力を混ぜない。利用者は別host terminalからsnapshotを取得する。

## cross-agent review

`.github/pull_request_template.md`へ次を固定する。

- change summaryとsecurity boundary
- implementation agentとreview agent（同一／未実施を明記可能）
- automated testsと実host gate
- credential非露出、破壊操作、残存リスク

CIはtemplateと運用文書の必須heading／語句をunit testで検証する。agent名やreview結果を自動的に真実とは判定せず、空欄をCIで強制もしない。人間がPR本文をreviewするための標準形式であり、AI reviewやtest成功だけをmerge判断にしない。

## 受け入れ条件

- Codex／Claude runtime specにexact project/agent labelがある。
- `agentctl stats PROJECT`がlabel一致containerだけを固定fieldで表示する。
- 0件、Podman failure、不正projectをfail closedに処理する。
- PR templateとoperator documentationがCIで検証される。
- unit suite、Podman CI、実hostのrunning／not-running stats smokeが成功する。
