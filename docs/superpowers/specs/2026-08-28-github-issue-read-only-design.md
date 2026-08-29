# GitHub Issue read-only broker設計

## 目的

既存のproject-scoped GitHub App brokerへ、選択中repositoryのIssueを読み取る最小機能を追加する。container内のagentへGitHub credential、任意API proxy、write権限を渡さず、Issue一覧と単一Issue詳細だけを固定commandと固定response schemaで提供する。

この変更は既存のGit transportとPull Request操作を維持する。Issueの作成、編集、comment、close、lock、delete、検索、任意query、任意paginationは対象外とする。

## command contract

container内では既存の`agent-github` wrapperへ次を追加する。

```sh
agent-github issue list
agent-github issue view NUMBER
```

`issue list`はopen Issueを作成日時の新しい順に最大30件返す。filter、sort、direction、page、limitを利用者が指定するoptionは設けない。`issue view`は正の整数のIssue番号だけを受け取る。repository、owner、API URL、HTTP headerを引数や環境から受け取らない。

broker protocolへ次のoperationを追加する。

- `issue-list`: payloadは空objectだけを許可する。
- `issue-view`: payloadは`number`だけを持ち、値は1以上2,147,483,647以下の整数とする。booleanは整数として受理しない。

unknown field、unknown operation、追加optionはbrokerがGitHubへ接続する前に拒否する。

## architectureとdata flow

Issue APIは新しい`github_issue.py`へ分離する。このmoduleはGitHub App installation token providerを既存実装と共有するが、既存の`github_pr.py`をIssue対応へ一般化せず、PR write経路を変更しない。

data flowは次のとおりとする。

1. `agent-github`が固定CLIをparseし、runtime環境からsocket、capability、project IDを取得する。
2. clientがnonce付きの`issue-list`または`issue-view` requestを既存length-prefixed protocolで送る。
3. brokerがpeer UID、capability、project、nonce、operation、payloadを検証する。
4. Issue handlerがsessionに固定されたrepositoryからだけinstallation tokenを取得する。
5. GET専用transportが固定pathへrequestする。
6. handlerがGitHub responseを厳格に検証し、allowlist済みfieldだけのJSONへ縮小する。
7. brokerがresponse sizeを再確認してchunk streamで返し、secret-free auditを記録する。
8. clientが固定schemaを検証したJSONだけをstdoutへ1行で出す。

Issue transportが生成できるendpointは次に限定する。

- list: `/repos/OWNER/REPO/issues?state=open&per_page=30&sort=created&direction=desc`
- view: `/repos/OWNER/REPO/issues/NUMBER`

methodは`GET`だけを許可する。redirectは追従しない。GitHub raw response、response header、token、任意error本文はcontainerへ返さない。

## GitHub App permission

installation token要求へ`issues: read`を追加する。既存の`contents: write`、`pull_requests: write`、`checks: read`、`metadata: read`は変更しない。token responseのpermission objectはこの完全一致だけを受理し、欠落、余分なpermission、異なるlevelを拒否する。

GitHub App側のinstallation permission更新が必要である。設定更新と再承認を実host smokeの事前条件として文書化し、permissionが不足する場合に別credentialやlegacy `gh`へfallbackしない。

## response schema

list responseは次の固定形とする。

```json
{"issues":[{"number":1,"title":"Example","state":"open","author":"octocat","labels":["bug"],"created_at":"2026-08-28T00:00:00Z","updated_at":"2026-08-28T01:00:00Z","url":"https://github.com/OWNER/REPO/issues/1"}]}
```

view responseは同じfieldに`body`を追加する。

```json
{"number":1,"title":"Example","state":"open","author":"octocat","labels":["bug"],"body":"Issue body","created_at":"2026-08-28T00:00:00Z","updated_at":"2026-08-28T01:00:00Z","url":"https://github.com/OWNER/REPO/issues/1"}
```

validation ruleは次のとおりとする。

- `number`はrequestと同じ範囲の整数。
- `title`は空でないUTF-8文字列で最大256 bytes。NULと不正control characterを拒否する。
- `body`はUTF-8文字列またはGitHubの`null`を許可し、`null`は空文字列へ正規化する。最大256 KiBとする。
- `state`は`open`または`closed`だけを許可する。
- `author`はGitHub user objectの`login`または、削除済みuserを表す`null`だけを許可する。
- `labels`はlabel objectの`name`だけへ縮小する。各nameは最大100 bytes、最大100件とする。ID、色、説明は返さない。
- `created_at`と`updated_at`は秒精度または小数秒を含むUTCのISO-8601 `Z`形式を検証する。
- `url`は`https://github.com/OWNER/REPO/issues/NUMBER`との完全一致だけを許可する。
- response全体は2 MiB以下とする。

list endpointはPull Requestも返すため、itemに`pull_request` fieldがある項目を除外する。残るIssueはGitHub responseの順序を維持する。入力配列が30件を超える場合、itemがobjectでない場合、Issue itemの必須fieldが欠落または不正な場合は部分結果を返さずfail closedとする。GitHub responseの追加field自体は無視し、出力へはallowlist済みfieldだけを含める。request payloadの追加fieldはGitHubへ接続する前に拒否する。

## error handling

clientは既存と同じ固定messageだけをstderrへ出し、exit 1を返す。

```text
error: GitHub broker request failed
```

GitHubのstatus、body、header、URL、token、response断片をstdout、stderr、auditへ出さない。transport error、timeout、redirect、non-JSON、content type不一致、oversize、schema不一致はすべてfail closedとする。

401 responseではinstallation token cacheを1回だけinvalidateし、同じ固定requestを1回だけ再試行する。2回目の401と、401以外のHTTP failureは再試行しない。write methodや別endpointへfallbackしない。

broker failure stageへ`issue-request`を追加する。既存Git、PR、token stageの意味は変更しない。

## auditとsecret境界

auditへ記録できるfieldは既存固定metadataに次を加えたものだけとする。

- operation: `issue-list`または`issue-view`
- status
- `issue-view`の場合だけIssue番号
- responseの転送byte数
- error時の固定stage `issue-request`

title、body、author、label、URL、GitHub response、token、capability、request環境を記録しない。Issue番号用fieldはPR番号と意味を混在させず、`issue_number`として分離する。

## lifecycleとsecurity boundary

Issue operationは既存broker sessionのpeer UID、runtime capability、固定project、nonce replay防止、socket lifecycleをそのまま使用する。runtime終了時にsocketとcapabilityを破棄し、stale clientを拒否する。

次を新たに提供しない。

- Issue create、edit、comment、close、reopen、lock、unlock、delete、label変更、assignee変更
- arbitrary search、query、pagination、GraphQL、generic REST proxy
- cross-repository read
- raw GitHub responseまたはheader
- legacy GitHub CLI credentialへのfallback

## automated testing

test-firstで次を検証する。

- CLI parserとrequest payloadが固定operationと固定schemaだけを生成する。
- Issue番号、title、body、state、author、label、日時、URL、response sizeの正常値と境界値。
- listがPR itemを除外し、Issue順序を維持する。
- requestのunknown field、redirect、非JSON、oversize、別repository URL、不正author、不正日時をfail closedにする。
- 401時だけtokenを1回更新し、その他をretryしない。
- installation token permissionが既存権限と`issues: read`の完全一致である。
- auditに本文などのsentinelが含まれない。
- create、edit、comment、close、lock、delete、search、query、pagination operationが存在しない。
- 実Unix socket integrationでlistとview responseがclientまで届き、runtime終了後のcapabilityを拒否する。
- 既存Git upload／receive-packとPR create／view／checksのtestが無変更で通る。

通常の全test suiteと`git diff --check`を必須gateとし、実GitHub credentialや実Issueを使うcheckは自動CIの成功として推測しない。

## documentationと実host smoke

operator guideへcommand、fixed schema、permission、非目標、failure behaviorを追加する。実host smoke checklistは既存Issueまたは利用者が事前指定したfixtureをreadするだけとし、test用Issueを自動作成しない。

実hostでは次を確認する。

- GitHub Appに`issues: read`が付与され、token responseが厳密に受理される。
- listがopen Issueだけを最大30件返し、PR itemを含まない。
- viewが指定Issueの固定fieldと本文を返す。
- cross-repository、unknown operation、write相当operationを拒否する。
- stdout、stderr、auditにtoken、capability、raw response、検査sentinelがない。
- runtime終了後のclientを拒否する。
- 既存clone、fetch、push、PR create／view／checksが回帰していない。

観測結果は実装PR merge後の別PRへ記録する。未実施の実host項目をPASSとして書かない。

## rolloutとrollback

実装はIssue module、policy operation、broker dispatch、client CLI、token permission、tests、docsを同じfeature PRで追加する。CIと独立security reviewがPASSしてからmergeし、その後に実host smokeを行う。

rollbackではIssue operation、`issues: read` token要求、Issue module、CLI routeをまとめて戻す。既存PR moduleを再構成しないため、Git transportとPR操作を独立して維持できる。permission不足や実host failure時にcredential mount、generic API、unsandboxed fallbackを追加しない。
