# Phase 3 resource監視・cross-agent review実装計画

## 1. Runtime label

`tests/container/test_podman.py`へCodex／Claude runtimeのmanaged、project、agent labelを要求する失敗testを追加する。`src/agent_container/podman.py`のruntime prefixへ、検証済みproject IDと固定agentからlabel引数を追加する。cloneやauthなど一時containerへは付けない。

## 2. Stats command spec

Podman command unit testを先に追加し、label一致running IDを取得する`podman ps --filter label=... --format {{.ID}}`と、IDを明示した`podman stats --no-stream`の固定format specを実装する。shell、latest、任意formatを使わない。

## 3. agentctl stats

`tests/container/test_agentctl.py`へparser、0件、複数件、Podman error、成功出力、secret非露出testを追加する。`agentctl stats PROJECT`を実装し、rootless preflight、ID形式／件数上限の検証、固定header付きsnapshot出力を行う。

## 4. Review templateとdocs

`.github/pull_request_template.md`とresource/review運用節を追加する。`tests/container/test_docs.py`へ必須heading、agent reviewの非保証、secret-free stats field、rootless/cgroups境界のtestを先に追加する。READMEとCHANGELOGから運用文書へlinkする。

## 5. Verification

focused tests、`PYTHONPATH=src python3 -m unittest`、`git diff --check`を実行する。独立reviewでCritical/Importantを解消後、hostでCodexまたはClaude runtimeを起動し、別terminalからrunning stats成功と終了後のnot-running failureを確認する。credential値、環境、argvは記録しない。
