# Phase 3 resource監視・cross-agent review運用

## Resource snapshot

CodexまたはClaudeを実行したhostとは別のterminalで、次を実行します。

```bash
bin/agentctl stats PROJECT
```

出力は`AGENT`、container ID、`CPU`、`MEMORY`、`PIDS`、`UPTIME`だけです。環境変数、mount、process argv／command、credential、network I/Oは取得・表示しません。対象はmanaged、project、agent labelに一致する実行中containerだけです。通常運用では別project、停止済みcontainer、labelなしの一般Podman containerを含みません。

labelは認証された所有証明ではなく、同じrootless Podman user内のdiscovery selectorです。同じuserが任意containerへ同じ公開labelを付ければsnapshotへ混入できます。`stats`は新しい情報アクセス権を与えず、そのuserが元々`podman ps/stats`で読めるresource値だけを表示しますが、敵対的なsame-user containerが存在するhostではsnapshotを真正性の証拠として扱いません。

`stats`はsnapshotを1回取得して終了します。継続監視が必要なら利用者が明示的に再実行します。Codex／ClaudeのTUIへ非同期出力を混ぜません。

rootless Podmanのresource statsはcgroups v2を前提とします。対応していないhost、Podman error、対象containerなし、形式不正ではnonzeroでfail closedになり、別containerや`--latest`へfallbackしません。rootless環境ではnetwork統計に制約があるため、そもそも表示対象にしません。

## Cross-agent review

PRは[共通template](../.github/pull_request_template.md)を使い、implementation agent、review agent、security boundary、automated test、実host gate、外部変更、残存リスクを記録します。同一agentによるreviewやreview未実施は隠さず明記します。

AI reviewだけで正しいと断定しません。test成功もsecurity boundary、仕様適合、運用リスクの人間reviewを置き換えません。credential値やprivate transcriptをPR本文へ記録せず、PASS/FAIL、exit status、公開version、固定operation名などsecret-freeな証拠だけを残します。

## Smoke checklist

1. Codex runtimeを起動し、別terminalの`agentctl stats PROJECT`がCodex 1件だけを表示する。
2. Claude runtimeでも同じ確認を行う。
3. 別project指定が対象containerを表示しない。
4. runtime終了後は対象なしでnonzeroになる。
5. 出力に環境変数、argv、mount、credentialがない。

未実行項目は`not run`のまま記録し、unit testを実host観測として扱いません。

## 実host検証記録

2026-08-26にrootless Podman hostで確認しました。

| check | observed |
| --- | --- |
| Codex running snapshot | PASS: `AGENT CONTAINER CPU MEMORY PIDS UPTIME`の固定fieldだけを表示 |
| Claude running snapshot | PASS: 同じ固定fieldだけを表示 |
| runtime終了後 | PASS: `no running agent container found for project`、exit 1 |
| secret-free boundary | PASS: 環境変数、mount、argv、credential、network I/Oを表示せず |

snapshotのcontainer IDと瞬間的なresource値は運用結果へ保存していません。公開labelは非認証selectorであるため、same-user spoofingに対する真正性の証拠にはしません。
