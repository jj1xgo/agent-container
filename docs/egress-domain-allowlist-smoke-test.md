# Runtime egress allowlist smoke test

この手順は実外部操作ごとに停止し、対象project、exact domain、agent、期待する操作を示してfresh approvalを得てからだけ続行します。approvalは次のblockへ持ち越しません。shell tracingを無効にし、token/header/body/TLS/DNS dumpを取得・表示・保存しません。失敗時のfallback、無断retry、unobserved PASSは禁止です。

## 1. Local preflight

このblockは外部serviceへ接続しません。

```bash
set +x
agentctl project configure-egress PROJECT --enable
agentctl project configure-egress PROJECT --add-domain pypi.org
agentctl doctor PROJECT
```

doctorはlocal doctorの範囲だけを評価します。ここでは実socket/Podman/serviceをPASSとしません。結果は`PASS`、`PARTIAL`、`FAIL`、`not run`のいずれかで、実行command、exit code、日付、OS/Podman/versionなどのnon-secret environment metadataだけを記録します。env vars/valuesはcredentialでないと推測せず、一切記録しません。

## 2. External discovery gate

ここで停止します。CodexまたはClaudeが必要とするdomain discoveryを行う前にfresh approvalを取得してください。承認されたagentの最小操作を一度だけ実行し、完全なDNS/TLS/network logを保存しません。観測したexact domain候補はcredentialを含まない形で別reviewへ渡し、このblock中にはpolicyへ追加しません。

## 3. Approved runtime gate

ここで再度停止します。review済みexact domainと対象agentを提示してfresh approvalを取得した後だけ実行します。

```bash
agentctl run PROJECT
```

成功、拒否、gateway cleanup、no fallbackを観測します。認証内容、prompt/response本文、header、TLS plaintextは記録しません。未観測項目は`not run`または`PARTIAL`であり、unobserved PASSにしません。

## 4. Remove and rollback

外向き制限を変更する操作なので、各command前にfresh approvalを取得します。

```bash
agentctl project configure-egress PROJECT --remove-domain pypi.org
agentctl project configure-egress PROJECT --disable
```

`--disable`後は次回runtimeが制限前networkへ戻ります。rollback consequenceを結果へ明記し、無断でruntimeを再起動しません。
