# Egress sequence diagnosis — 2026-09-05

## Follow-up (2026-09-05)

独立修正PR #107をmain `7a9e927`へmerge済み。以下は修正前の基準`8cb0422`／`v0.5.0`での診断記録です。reproducerは旧挙動をassertするため、修正版の期待結果を表しません。修正版の認証済み実host smoke 1回では拒否requestより大きいsequenceの受理とauthentication denial 0件を観測しましたが、新たな未許可domainで停止し、最小応答は未確認です。詳細はCHANGELOGの後続記録を参照してください。

## Scope and status

Phase 6-6の利用者承認済み診断。production・既存test・許可policyは変更しない。
実Codexを1回だけ起動し、追加許可を`chatgpt.com`／`pypi.org`に維持した。
基準は`8cb0422`（productionは`e2ce4a9`と同一）、imageは`129ea06302ee`。
実runtime gateはFAILのまま。Issue登録、追加domain、再実行、修正は未実施。

## Host observation

- `github.com`のpolicy拒否時に、container内でproxy向けESTABLISHED socketのinodeと
  processのfdを照合した。socket保持者は`git-remote-http`のみ、祖先には
  `git`、`codex`、`node`、`python3.14`があり、Git操作の固定語は`ls-remote`だった。
- 完全なargv・環境・URL・通信本文は記録せず、既知の実行名と固定操作語だけを収集した。
  先行調査でproject config内に既知superpowers marketplaceのGitHub URLを確認済み。
  Codex起動中のGit参照確認であることは観測したが、対象repositoryや呼び出し目的を
  raw argvから特定する診断は行っていないため、marketplace更新との対応は推定に留まる。
- 認証拒否8件はすべて元の`authorize`が出した固定のsequence不一致エラーだった。
  その他の認証拒否は0件。元の判定はそのまま実行し、再試行・許可のfallbackはない。
- 作成tunnel 0件、auditはpolicy denial 1／sequence由来authentication denial 8件。
  最初の拒否後に停止したので、停止後process exit 0を推論成功には数えない。
- container／broker thread／run directoryの回収、policy byte不変、audit固定fieldを確認。
  auditのbyte-count fieldは既存の`bytes_from_client`／`bytes_from_upstream`を検査した。
- process照合は拒否応答前に一度だけ行い、タイミングに影響し得る。
  本番の発生頻度は測定していない。以下のlocal reproductionはprocess観測に依存しない。

## Cause and deterministic reproduction

`egress_adapter.EgressSequence.next`は要求送信前にclient側counterを進める。
一方`egress_broker.EgressBrokerSession.authorize`は厳密な次連番を要求し、
policy検証に通った場合だけcounterを進める。拒否された要求により両者がずれる。
またadapterはclient毎のthreadから送信するため、counter割当順と到着順が
一致する保証がない。先に到着した大きい番号を拒否すると、その後も連番がずれる。

同じsynthetic reproductionを現在のsrcと`git archive v0.5.0 src`でexportしたsrcで実行した。
公開tagのcommitは`1963298c4f94e19b1b95f7b1ff2be453339ebed7`。
両方で次が一致し、network・Podman・実credentialは使用していない。

| Input | Observed on current and v0.5.0 |
| --- | --- |
| denied domain seq1 → allowed domain seq2 | policy-denied → sequence-denied |
| allowed seq2 → allowed seq1 → allowed seq3 | sequence-denied → allowed → sequence-denied |

このreproducerは不具合の現状をassertする診断コードであり、修正後の期待値を表す回帰testではない。
新しいテストを成功済みsuiteに数えていない。実行は`PYTHONPATH=<対象src> python3 <このコードの保存先>`。

```python
import json,os,tempfile
from pathlib import Path
from agent_container.state import StateLayout
from agent_container.egress_policy import EgressPolicy
from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_adapter import EgressSequence
from agent_container.egress_broker_protocol import EgressRequest

def trial(order):
 with tempfile.TemporaryDirectory(prefix='e66-') as temp:
  root=Path(temp)/'s';root.mkdir(mode=0o700)
  session=EgressBrokerSession.create(StateLayout(root,'probe'),'codex',EgressPolicy(1,'allowlist',('allowed.example.com',)))
  sequence=EgressSequence()
  def request(domain): return EgressRequest(1,session._capability,'probe',sequence.next(),'connect',domain,443)
  def attempt(req):
   try: session.authorize(req,os.getuid()); return 'allowed'
   except ValueError as e:
    if getattr(e,'stage',None)=='policy': return 'policy-denied'
    if str(e)=='egress broker request sequence is invalid': return 'sequence-denied'
    return 'other-failure'
  try:
   if order=='policy': observed=[attempt(request('denied.example.com')),attempt(request('allowed.example.com'))]
   else:
    first,second=request('allowed.example.com'),request('allowed.example.com')
    observed=[attempt(second),attempt(first),attempt(request('allowed.example.com'))]
   return observed
  finally: session.close()
result={'denial_then_allowed':trial('policy'),'reordered_valid_requests':trial('reordered')}
assert result=={'denial_then_allowed':['policy-denied','sequence-denied'],'reordered_valid_requests':['sequence-denied','allowed','sequence-denied']}
print(json.dumps(result))
```

## Follow-up boundary

Phase 6 stage 1の振る舞い保存refactor／smoke記録とは別の修正として扱う。
単にcounter更新をpolicy検証前へ移すだけでは到着順逆転を解決できない。
修正設計では認証済みrequestの再送拒否、順序逆転、拒否後の許可済みrequest、
未認証requestによるstate消費禁止、追跡stateの上限と使い切り時の挙動を合わせて決める。
成功するまでdomainを追加する対応でこの問題を隠さない。
