# Phase 1 Claude限定handover writer設計

## 目的

Claude Codeのnested sandboxを弱めず、選択中projectに新しいhandoverを作成できるようにする。現在のouter Podman runtimeはproject handover directoryをread-write mountしているが、Claude固有のnested sandboxはworkspace外への直接書き込みを拒否する。Phase 1ではこの拒否を迂回せず、host側の限定writerだけに新規作成を委譲する。

この変更はClaudeだけを対象とする。Codexの既存handover mountと`agent-handover`直接作成経路は維持し、両agentの共通control plane化はPhase 2へ分離する。GitHub Issue read-only対応も別設計・別PRとする。

## 非目標

- Claudeへhandover directoryの任意write、rename、overwrite、delete権限を渡さない。
- 既存handoverの編集、削除、一覧取得、本文取得をbroker operationに追加しない。
- outer Podman sandbox、Claude nested sandbox、managed policy、credential隔離を弱めない。
- GitHub brokerのprotocolまたはcredential境界へhandover operationを混在させない。
- transcript全文、credential、session/cacheをhandoverへ自動保存しない。
- Phase 1でCodexのhandover経路を移行しない。

## 選択した方式

runtime限定の専用Unix-socket brokerをhost側で起動する。GitHub brokerとはsocket、capability、protocol、audit、lifecycle objectを分離する。Claude containerへは次だけを渡す。

- 選択projectのhandover directoryをread-only mountした`/handovers/PROJECT`
- read-onlyのbroker socket mount
- read-onlyのruntime限定capability file
- project IDと固定broker pathを示す非secret環境設定

workspace内へhandover directoryを別名でwrite mountする方式は採用しない。この方式ではnested sandboxを通過できても、任意作成、既存file上書き、rename、deleteまで許してしまう。GitHub brokerへoperationを追加する方式も、GitHub credential境界とlocal file writerの責務を不必要に結合するため採用しない。

## command contract

Claudeは既存と同じcommand形を使う。

```sh
agent-handover create --title "引き継ぎタイトル"
```

Claude runtimeでのwrapperは完成したMarkdown section本文をstdinから読み、project ID、broker socket、capabilityをruntime環境から取得する。保存先、project、session、任意pathをcommand lineで指定するoptionは提供しない。Codex runtimeではbroker環境を設定せず、現行の直接writer contractを維持する。

stdinは次の7 headingを各1回、固定順序で含む。各section本文は空でもよく、`###`以下のsubheadingを使用できるが、他の`##` heading、headingの省略、重複、並べ替えは拒否する。

1. `## 作業の目的`
2. `## 現在地`
3. `## 決定事項と理由`
4. `## 変更したファイル・commit・PR`
5. `## 検証結果`
6. `## 未解決事項とリスク`
7. `## 次の一手`

H1とmetadataはstdinへ含めない。host writerが検証済みtitleとruntime登録情報から次をcanonical生成する。

- `# Handover: TITLE`
- `Project`
- timezone付きUTC `Created`
- `Session`

Claudeのsession IDをhostが信頼できる形で取得できない間は`Session`を`（未記録）`とする。request本文やcontainer環境から任意session IDを受け入れない。成功responseはcontainerから見える作成pathだけとし、本文を返さない。

## protocolと認証

protocolは固定上限を持つlength-prefixed JSON request/responseとする。request schemaはversion、operation `create`、project ID、title、section本文、capabilityだけに限定し、未知fieldを拒否する。文字列はUTF-8で、NULを拒否する。request frameと生成後handoverはいずれも64 KiB以下に制限する。

brokerは次をすべて満たすrequestだけを処理する。

- Unix socket peer UIDがruntimeを開始したhost UIDと一致する。
- capabilityがruntime生成値とconstant-time比較で一致する。
- request project IDがruntime登録projectと完全一致する。
- operationとschemaがallowlistに一致する。
- runtimeがactiveで、同じsocket instanceに対応している。

capabilityはruntimeごとにcryptographically randomな値を生成し、host private runtime directoryへ`0600`で保存する。socket、capability、runtime directoryはClaude終了時に破棄する。broker failure時にdirect writerまたはread-write mountへfallbackしない。

## validationとsecret境界

host writerはfilesystemへ触れる前に次を検証する。

- project IDは既存のsafe repository-style slug validationを通る。
- titleはtrim後に空でなく、CR/LFを含まない。
- section headingは必須7件と完全一致し、固定順序で各1回だけ現れる。
- bodyはUTF-8、NULなし、size上限内である。
- H1またはmetadata偽装用の先頭内容を受理しない。
- `ghp_`、`github_pat_`、`sk-`、`BEGIN PRIVATE KEY`など既知credential markerを本文とtitleの両方で拒否する。

secret marker検査はcredential非混入の完全な保証ではなく、既知の高risk文字列をfail closedで止める補助境界である。Claude向けinstructionsでも、環境一覧、token、credential値、transcript全文を本文へ入れない規則を維持する。

host側は登録済みhandover rootとproject directoryをstrict resolveし、symlink、traversal、通常directory以外を拒否する。requestからrootまたはpathを受け取らない。

## atomic create

writerは既存のUTC秒精度、一意suffix付きfilename規則を維持する。project directory内に推測困難な名前のprivate temporary fileを`O_CREAT|O_EXCL|O_NOFOLLOW`、mode `0600`で作り、完全なcanonical documentを書いてflushと`fsync`を行う。その後、temporary fileから最終filenameへのhard linkを作ることで、replaceを許さずatomic publishする。publish先が存在した場合は新しいsuffixで最大8回だけ再試行する。

publish成功後にtemporary filenameをunlinkし、project directoryを`fsync`する。validation error、write error、disconnect、publish collision上限、broker shutdownのいずれでも最終filenameへpartial documentを残さず、temporary fileもcleanupする。既存handoverをreplaceするoperationは実装しない。

## runtime lifecycle

`agentctl run PROJECT --agent claude`のpreflightは、現在と同じproject metadata、handover root、project directory、owner、mode、symlink境界を検査する。成功後にhandover broker runtimeを開始し、socketとcapability mountをClaude command specへ渡す。

Claude command specはhandover project mountをread-onlyへ変更する。workspace、Claude config、cache、token、GitHub mode、outer read-only container、capability drop、no-new-privileges、keep-id、PID namespace、tmpfsは現行境界を維持する。GitHub brokerの有無とhandover brokerの起動可否は独立であり、handover broker開始に失敗したらClaude runtime全体を起動しない。

Claude process終了、起動失敗、signal、例外の各経路でbrokerを停止し、socket、capability、temporary runtime directoryをcleanupする。cleanup失敗はsecret-free errorとして報告し、次回runtimeで古いcapabilityを再利用しない。

## auditとerror handling

auditはtimestamp、project、operation、固定stage、成功・拒否、作成pathだけを記録できる。request本文、title、capability、credential marker一致部分、環境値、raw exception、socket payloadを記録しない。

clientへ返すerrorは固定codeとsecret-free messageに限定する。少なくともauthentication、schema、size、content-policy、filesystem-boundary、write、unavailableを区別する。内部例外文字列をそのまま返さない。clientはnonzeroで終了し、直接writeを試さない。

## Claude instructions

Claude向けmanaged instructionを追加し、別sessionまたは別agentへ引き継ぐ必要がある場合だけhandoverを作る。作成前にGit status、直近commit、実行済みtest、未解決事項を確認し、推測した成功を書かない。完成した7 sectionをstdinで一度だけ送信し、成功pathを確認する。

credential、環境値、transcript全文を含めず、broker拒否時にsandboxやmountを弱めたり、別pathへhandoverを作ったりしない。Codex向け既存skillはPhase 1では変更しない。

## doctorと運用文書

Claude doctorへ、image内client、runtime broker support、handover project read-only boundaryをsecret-freeに検査する項目を追加する。runtime限定socketは通常doctor時に存在しないため、永続socketの存在をPASS条件にしない。

operator guideはcommand contract、read-only mount、failure時の停止、Codexとの差分を説明する。実host smoke checklistは認証済みClaudeで次を確認する。

- 完成handoverを新規作成でき、pathだけが返る。
- canonical metadataと7 sectionが保存される。
- handover directoryの直接write、既存file上書き、rename、deleteがnested sandboxまたはread-only mountで拒否される。
- 他project handoverがmountにもbroker operationにも現れない。
- invalid capability、malformed body、secret markerでfileが作成されない。
- stdout、stderr、auditに本文、capability、credential由来情報が出ない。
- runtime終了後にsocketとcapabilityが再利用できない。

認証済みClaude、host Podman、実handover directoryを使うsmokeはunit testやCIの代替にしない。merge後に利用者の個別承認を得て実行し、未実施項目をPASSと記録しない。

## test strategy

TDDで次の層を実装する。

### validator unit tests

- valid titleと7 sectionを受理する。
- titleの空、CR/LF、oversizeを拒否する。
- malformed UTF-8、NUL、frame/body size超過を拒否する。
- section欠落、重複、順序違反、未知headingを拒否する。
- 既知credential markerをtitleとbodyのどちらでも拒否する。

### writer unit tests

- canonical metadata、UTC時刻、unique filename、mode `0600`を生成する。
- symlinked root/project、traversal、通常directory以外を拒否する。
- collisionを別suffixで再試行し、既存fileを変更しない。
- write、fsync、publish失敗でpartial final fileを残さずtemporary fileをcleanupする。

### broker/client tests

- valid peer、capability、project、schemaのcreateが成功する。
- invalid peer、capability、project、operation、未知fieldを拒否する。
- socket切断、oversize frame、malformed JSON／UTF-8をfail closedにする。
- responseとauditへ本文、capability、credential markerを出さない。
- clientはbroker failure後にdirect writeへfallbackしない。

### runtime and integration tests

- Claude command specがhandover projectをread-only mountし、socket/capabilityだけを追加する。
- Codex command specと既存direct writerが変わらない。
- GitHub broker有無の両方でhandover broker lifecycleが独立して動く。
- 実Unix socketでclientからhost writerまで新規handoverを作成する。
- runtime終了後のcapability再利用を拒否する。
- 全unit suite、GitHub Actions Unit tests、Podman integration、`git diff --check`を通す。

## acceptance criteria

- Claudeはnested sandboxとouter Podman制約を維持したまま、選択projectへ新規handoverを作成できる。
- Claudeはhandover directoryを直接変更できず、brokerもcreate以外を提供しない。
- hostがproject、metadata、filename、atomic publishを決定する。
- invalid requestとbroker failureはfileを作らずfail closedになる。
- Codexの既存handover behaviorに回帰がない。
- CIと独立reviewが成功し、実host smokeの未実施境界が明記される。
