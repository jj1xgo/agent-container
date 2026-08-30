# Runtime egress exact-domain allowlist 運用ガイド

この機能はprojectごとのopt-inです。有効にした通常のCodex/Claude runtimeだけを`--network=none`で起動し、追加したexact-domainのTCP 443をprivate gateway経由で許可します。wildcard、IP literal、Unicode/punycode、local/reserved domain、443以外は拒否します。build/auth/update、setup、project登録、host側GitHub broker通信は対象外です。

policy fileはprojectのmode `0700` directory内にmode `0600`で保存されます。runtime capabilityは一回のrun専用で、gatewayはTLS plaintext、HTTP header/body、token、domain、IPをauditしません。TLSはcontainerからremote peerまでend-to-endです。policy、adapter、gatewayが失敗した場合はno fallbackで終了し、通常networkへ再起動しません。

## 設定とrollback

対象project名を`PROJECT`へ置き換え、1操作ずつ結果を確認します。

```bash
agentctl project configure-egress PROJECT --enable
agentctl project configure-egress PROJECT --add-domain pypi.org
agentctl doctor PROJECT
agentctl run PROJECT
agentctl project configure-egress PROJECT --remove-domain pypi.org
agentctl project configure-egress PROJECT --disable
```

`--disable`がrollbackです。次回の通常runtimeは従来のnetworkへ戻るため、制限が解除されるsecurity consequenceを理解してから実行してください。実行中containerのpolicyは変更されません。追加・削除は小文字ASCIIの完全一致domainだけを受け付け、managed production core domainsは別のreal-host evidenceとreviewなしに追加しません。

## doctorと証拠

local doctorはpolicy schema、private state、managed adapter self-checkを確認しますが、実Unix socket relay、rootless Podman enforcement、public service到達性、実inferenceの証明にはなりません。

- `WARN`: policyが未設定でruntime networkが制限されない、またはrelease evidenceが不足している。
- `PASS`: 指定したgateをcapable environmentで観測し、exit 0と期待する副作用を確認した。
- `PARTIAL`: 一部gateだけPASSし、残りを明記した。
- `FAIL`: product contract違反、または実行したgateが非zeroになった。
- `not run`: 環境不足またはfresh approval待ちで実行していない。PASSへ読み替えない。

CIはsocketとPodmanのlocal fixtureをpublic internetやcredentialなしで実行します。実serviceを使う確認は[smoke guide](egress-domain-allowlist-smoke-test.md)に従います。
