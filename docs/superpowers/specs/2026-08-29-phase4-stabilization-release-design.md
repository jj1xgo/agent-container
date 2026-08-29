# Phase 4 scope整合・残存gate・v0.4.0安定化設計

- 日付: 2026-08-29
- 状態: 承認済み
- 対象: 初期設計とのscope整合、専用test repositoryによる残存実host gate、`v0.4.0` release

## 1. 背景

Phase 1からPhase 3で、Codex／Claude Codeの分離runtime、project別state、handover、GitHub App broker、CI、resource監視、cross-agent reviewを実装した。主要な自動testと実host smokeは成功している。一方、初期設計には現行の安全方針と一致しないfamily Issue writeが残り、外向きnetworkのdomain allowlistは未実装である。また、実repositoryにIssueがなかったため、Issue view、body、Pull Request除外、非空payloadの情報制限、stale client拒否などは実hostで完全に観測できていない。

Phase 4では新しい権限やplatformを追加しない。設計と現行interfaceを一致させ、専用test repositoryで安全に再現できる残存gateを閉じ、証拠と残存制約を固定したうえで`v0.4.0`をreleaseする。

2026-08-29のnegative gateで、private repositoryのruleset inventoryはHTTP 403のupgrade-or-public制限となり、unrelated-history force pushが受理された。これを受け、broker pushはGitHub設定に依存しないcreate-onlyへ修正する。advertisementに存在しないunprotected branchをold OID zeroで作る場合だけ許可し、既存branchへのupdateを拒否する。fast-forwardもnon-fast-forwardも拒否し、追加作業は新しいbranchと必要に応じた新しいPRを使う。

## 2. 監査結果

| 分類 | 項目 | 根拠と扱い |
| --- | --- | --- |
| 完了 | rootless Podman、read-only root、capability削除、限定mount | code、unit／integration test、Phase 1／2実host smoke |
| 完了 | project別workspace、state、cache、設定分離 | codeと境界test |
| 完了 | Codex認証、run、resume、handover、statusline | Phase 1実host smokeと後続regression |
| 完了 | Claude認証、managed sandbox、編集、test、commit、resume | Phase 2実host smoke |
| 完了 | Claude allowlist migrationとplugin選択 | code、test、disposable migration smoke |
| 完了 | exact repositoryのclone／fetch、通常push、PR create／view／checks | Phase 3実host smoke |
| 完了 | CI、Podman integration、resource snapshot、cross-agent review | workflow、test、運用文書 |
| 完了 | Issue list／viewのread-only interface | code、unit／socket test、空list実host smoke |
| PARTIAL | Issue view／body、PR除外、非空payloadの情報制限 | 実repositoryにIssueがなくdata依存gateを未観測 |
| PARTIAL | Issue stale client拒否 | runtime artifact cleanupは確認、stale request未実施 |
| 修正必須 | existing branch update拒否 | disposable branchへのunrelated-history force pushが成功したためcreate-only broker gateへ変更 |
| PARTIAL | token更新をまたぐ長時間session | 401時一度だけ更新する自動testは存在し、実時間expiryは未観測 |
| scope変更 | family repositoryのIssue create／comment | 現行interfaceは選択repository限定read-only。writeは将来Phaseへ延期 |
| 未着手 | outbound networkのdomain allowlist | 既知のdoctor WARNとして維持し、独立した将来Phaseへ延期 |
| 運用未完 | 対象repositoryを増やす段階的移行 | 機構は存在するが、利用者のrepository展開はprojectごとの運用判断 |
| 条件付き保留 | 英語文書、追加platform | 必要性が確認された場合だけ別途設計 |

実装計画内の未更新checkboxは完了証拠に使わない。判定は現行code、test、smoke記録、merge済み履歴を根拠とする。

## 3. Scope

### Phase 4に含める

- 初期設計を現行の権限境界へ更新する。
- family Issue create／commentを将来Phaseへ明示的に延期する。
- domain allowlist／egress proxyを独立した将来Phaseへ延期し、現行WARNを残す。
- 専用のprivate test repositoryを準備し、残存するGit／PR／Issue broker gateを実行する。
- smoke結果を`PASS`、`PARTIAL`、`not run`で証拠どおり記録する。
- 全自動test、実Podman integration、独立agent reviewを実施する。
- `CHANGELOG.md`とrelease文書を整え、最終承認後に`v0.4.0` tagとGitHub Releaseを作成する。

### Phase 4に含めない

- family Issue write brokerの実装。
- domain allowlist、egress proxy、透過network brokerの実装。
- macOS、Windows、Docker、Kubernetes対応。
- brokerへのmerge、release、repository管理、generic API追加。
- test repositoryの自動削除。
- credential、token、capabilityの値、長さ、prefix、suffix、hashの表示・記録。

## 4. 専用test repository

既定名は`jj1xgo/agent-container-smoke`とし、private repositoryとして作成する。repositoryは再現可能なfixtureと失敗調査のため保持し、自動削除しない。実際のownerまたは名前を変更する場合は、作成前に本設計と実装計画へ反映する。

固定fixtureは次のとおりとする。

- `main`: 初期READMEだけを持つ。
- open Issue: 固定title、body、label、milestoneを持つ。
- closed Issue: `issue view`のstateとbody確認に使う。
- open Pull Request: Issue listからの除外を確認できる固定sentinelを持つ。
- push smoke用work branch: 各runで一意な名前を使い、完了後に明示的に削除する。

Issue／PR番号を設計へ固定しない。host側の`$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`に番号と期待値を記録する。このmanifestはcredentialを含まない通常file、ownerは実行user、modeは`0600`とし、containerへmountしない。実行前に対象repositoryとfixture identityを照合する。raw responseの除外確認には、milestone titleやPull Request sentinelなど、許可fieldに含まれない固定文字列を使う。

## 5. External-state approval境界

次の操作は別々の外部状態変更であり、実行直前に対象、目的、影響を示して利用者の承認を得る。

1. private test repository作成。
2. GitHub App installation対象へのtest repository追加とpermission確認。
3. fixture Issue、label、milestone、Pull Request作成。
4. broker経由の通常pushとPR create。
5. smoke用work branchの削除。
6. release PRのmerge。
7. annotated `v0.4.0` tagのpushとGitHub Release作成。

broker失敗時にlegacy `gh` credential、environment token、SSH agent、host credential helperへfallbackしない。fixture準備などhost管理操作で`gh`を使う場合は、broker smokeとは明確に分離して記録する。失敗した外部操作を自動で再実行しない。

## 6. 実host gate

### 6.1 Local preflight

- rootless Podman、rebuild済みimage、Codex／Claude doctorの必須checkが成功する。
- test projectのmetadata、workspace origin、broker policyがexact repositoryに一致する。
- 既存GitHub App installationのselected repositoryへtest repositoryを追加し、他の選択を変更しない。要求permissionと実responseはcredential値ではなく許可名とlevelで確認する。
- create-only enforcementはbroker自身の不変条件であり、有料planのrulesetやbranch protectionを前提にしない。
- container内にlegacy `gh` credential、GitHub token環境変数、host Git credential store、SSH agentが存在しない。

### 6.2 Git／PR gate

- cloneとfetchが成功する。
- advertisementに存在しない一意なwork branchへの作成pushが成功する。
- 同じbranchへのfast-forward／non-fast-forward update、protected branch、delete、non-head ref、cross-repository操作がbrokerからGitHubへ送信する前に拒否される。
- stale leaseは自動testを必須証拠とする。advertisement後・RPC前のremote更新をcredential非露出のまま決定論的に同期できる方法を先にspikeし、成立した場合だけ実hostで実行する。race依存の並行pushは使わず、決定論的な方法が成立しなければ`PARTIAL`として理由と受容判断を記録する。
- negative操作はbrokerが送信前に拒否できるcaseを優先する。GitHubへ到達させる必要があるcaseもtest repository内だけで実行する。
- PR create／view／checksが成功し、merge、release、generic API interfaceが存在しない。

### 6.3 Issue gate

- `issue list`がopen Issueだけを固定schemaで返す。
- open Pull Request itemとそのsentinelがlist outputから除外される。
- `issue view`がopen／closed Issueの固定fieldとbodyを返す。
- milestoneなどの除外field sentinel、raw response、response header、credential由来情報をstdout／stderr／auditへ出さない。
- create、edit、comment、close、search、query、pagination、repository指定、generic APIがGitHub接続前に拒否される。
- 別repository、別project、invalid number、oversize、malformed responseがfail closedになる。

### 6.4 Cleanup／expiry gate

- runtime終了後にsocketとcapability fileが残らない。
- runtime中に準備したstale clientが終了後のrequestを成功させられない。
- stale client確認ではcapability本文や派生情報を表示しない。
- 401時のtoken更新は自動testを必須証拠とする。実時間expiryを待つ長時間testはrelease必須gateにしない。

### 6.5 記録

- stdoutは固定schemaの結果だけ、stderrは固定errorだけを記録する。
- auditはallowlist field名とstatus／stageだけを検査し、raw行を無制限に転載しない。
- 各checkへ日時、対象repository、期待結果、観測結果、`PASS`／`PARTIAL`／`not run`を記録する。
- 途中の失敗を後続成功で隠さず、root causeと最終再実行を区別する。

## 7. 自動検証とreview

release候補commitに対して次を実行する。

- 全unit tests。
- GitHub／handover Unix socket integration。
- 実Podman integration。
- 文書contract tests。
- `git diff --check`。
- image内version、実行file、Python source permissionの確認。
- Critical／Important findingが残らない独立agent review。

test成功は正しさの証明ではない。reviewではcredential、mount、network、filesystem、external-state、fallback、cleanup、rollbackの残存リスクを個別に評価する。

## 8. Documentation整合

初期設計、README、operator guide、smoke checklist、CHANGELOGを次の正本へ揃える。

- 開発repository操作はproject-scoped GitHub App brokerを使う。
- Issue操作は選択repositoryのread-only list／viewだけである。
- family Issue create／commentは未提供で、将来Phaseへ延期する。
- outbound networkはdomain allowlistされておらず、既知WARNである。
- host管理用`gh`とcontainer broker operationを混同しない。
- smokeの未観測項目をPASSとして扱わない。

## 9. Release gate

次をすべて満たした後でのみrelease承認を求める。

1. scope整合の文書変更と必要なtest変更がmainへmerge済み。
2. 専用test repositoryの安全に実施可能な必須gateが成功。
3. 未実施項目がある場合、理由、影響、受容判断が明記済み。
4. 全自動testとCIが成功。
5. Critical／Important review findingがない。
6. `CHANGELOG.md`に`v0.4.0`の変更と既知制約がある。
7. release対象commitがcleanな`origin/main`で特定済み。

利用者の最終承認後、release対象commitへannotated `v0.4.0` tagを作成してpushし、同じtagからGitHub Releaseを作る。tag先commit、Release URL、CI結果をread-onlyで再確認する。公開済みtagは移動、上書き、再利用しない。

## 10. Failureとrollback

- release前の失敗は対象gateで停止し、原因を修正した新commitで再検証する。
- merge後の問題は履歴を書き換えずrevert PRで戻す。
- 公開済みreleaseに問題がある場合もtagを移動せず、修正版を新しいversionとしてreleaseする。
- test repositoryとfixtureは再現用に保持する。
- App installation、fixtureなどhost側変更は、変更前後とrollback手順をcredential-freeに記録する。
- sandbox、mount、permission、network、credential境界を弱めるfallbackは採用しない。

## 11. Phase 4完了条件

- 初期設計と現行interfaceのscopeが矛盾しない。
- 専用test repositoryを使う残存gateの結果が記録されている。
- 自動test、CI、独立review、release gateが成功している。
- 残存制約がREADME、operator guide、CHANGELOGで一致している。
- 最終承認済みの`v0.4.0` tagとGitHub Releaseが存在し、tag先が確認済みの`origin/main` commitである。

Phase 4後の候補は、family専用Issue write brokerとdomain allowlist／egress controlである。両者は権限、threat model、運用が異なるため、Phase 4へ混在させず、それぞれ独立した設計承認を必要とする。
