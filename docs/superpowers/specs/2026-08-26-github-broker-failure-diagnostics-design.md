# GitHub Broker Failure Diagnostics Design

## Context

Phase 3の実GitHub App smokeで、broker経由cloneがremote helperのgeneric errorだけを返して停止した。Unix socket path、installation token permission、Smart HTTP upload-pack preambleという独立した実host差分を修正した後も、後続stageの失敗が同じerrorへ潰れている。

現在のbroker runtimeはconnection handlerから上がった例外をbroker thread全体のfailureとして保持する。upload-packはdiscovery後のRPCを含むtransport例外をconnection単位で処理せず、auditにも失敗stageを残さない。このため、credentialやGit response bodyを採取せずに実host failureを切り分けられない。

## Goal

GitHub brokerのsecurity boundaryを緩めず、token、Git discovery、Git RPC、response streamのどこで失敗したかを固定分類だけで観測できるようにする。1 connectionの外部・protocol failureはそのconnectionをfail closedにし、broker thread全体を予期せず停止させない。

## Non-goals

- GitHub response body、Git advertisement、packfile、commit内容をlogへ記録しない。
- exception本文、URL、HTTP header、JWT、installation token、private key、capabilityをlogへ記録しない。
- retry回数、timeout、permission、repository allowlistを緩和しない。
- generic API proxyや新しいGitHub操作を追加しない。
- この設計だけで現在のclone failure原因を推測して修正しない。

## Design

### Fixed failure stages

内部で使えるstageは次の固定集合だけとする。

- `token`: installation tokenの生成、要求、response検証
- `upload-discovery`: upload-pack advertisementのHTTP要求・検証
- `upload-rpc`: upload-pack RPCのHTTP要求・検証
- `receive-discovery`: receive-pack advertisementのHTTP要求・検証
- `receive-rpc`: receive-pack RPCのHTTP要求・検証
- `pr-request`: allowlist済みPull Request API要求・response検証
- `response-stream`: brokerとcontainer間のframingまたはstream転送

任意文字列をstageとして受け取らない。低層exceptionは固定stageを持つbroker専用exceptionへ変換し、元exceptionの本文とGitHub response bodyを保持・表示しない。

### Component boundaries

Token provider、Git transport、PR transportは、自身の境界で起きた既知の`ValueError`、`RuntimeError`、`OSError`を固定stageへ変換する。Git transportはtoken取得failureとHTTP/protocol failureを別stageとして扱う。

Connection handlerは固定stage exceptionとclient stream failureを捕捉する。認可前の不正requestは従来どおり`denied`とし、認可後の外部・stream failureは`error`とする。例外をbroker accept loopまで上げない。

予期しないprogramming errorは既存どおりbroker runtime failureとして表面化させる。`BaseException`や無制限な`Exception`をconnection handlerで握り潰さない。

### Audit schema

既存のsecret-free metadataに、`status`が`error`の場合だけ任意の`stage` fieldを追加できる。値は上記固定集合に限定する。

auditは引き続き次を記録しない。

- token、JWT、private key、capability、Authorization header
- request／response body、PR body、Git advertisement、packfile、commit内容
- exception本文、任意URL、environment値

`operation`、`status=error`、固定`stage`、UTC timestamp、run label、project、repositoryだけで診断する。

### Client behavior

discovery開始前のfailureでは既存のbounded response frameで`denied`または固定の`error` statusを返す。stream開始後のfailureでは接続を閉じ、remote helperはgeneric failureを返す。いずれもcredentialやhost detailをcontainerへ返さない。

brokerは1 connectionのfailure後も新しいconnectionをacceptできる。brokerそのもののlistener、cleanup、thread lifecycle failureだけをruntime全体のfailureとする。

## Testing

- token failureが`status=error, stage=token`だけをauditし、secret markerを含まない。
- upload discovery failureが`stage=upload-discovery`になる。
- upload RPC failureが`stage=upload-rpc`になり、connectionだけを閉じる。
- receive discovery／RPC、PR request、response streamも対応する固定stageになる。
- 不正capability、別project、別repositoryは外部failureではなく従来どおり`denied`になる。
- 固定集合外stage、`status!=error`とstageの組み合わせを拒否する。
- 失敗後の次connectionを処理できる。
- audit、stdout、stderr、exception、client responseにsecret markerやresponse body markerがない。
- 既存unit suite、Unix socket integration、Podman integrationを回帰実行する。

## Live verification

実装後、broker project登録を再実行する。失敗した場合は`events.jsonl`から最新の`operation`、`status`、固定`stage`だけを確認し、そのstageに限定して次のroot-cause investigationを行う。cloneが成功してもcredential非露出、exact repository拒否、fail-closed、push／PRの残りのsmoke gateを省略しない。
