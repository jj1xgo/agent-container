# Phase 6-3: egress broker を broker kernel に乗せ替える Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** egress broker の protocol（`egress_broker_protocol.py`）、session（`egress_broker.py`）、runtime（`egress_broker_runtime.py`）を `src/agent_container/broker/` の kernel（`frame`、`audit`、`runtime`）の上に乗せ替える。そのために kernel へ、egress が必要とする 4 つの追加（`FrameSchema.frame_label`、`create_private_file(mode=...)`、`SocketBrokerRuntime` の thread 方式・raw client・deactivate 順序・`wait_failed`、`open_connection`）を **追加のみ** で入れる。既存 egress test、wire byte、audit 行、error message、停止順序は一切変えない。

**Architecture:** kernel は broker 固有 module を import しない。`EgressBrokerSession` は authorize／tunnel 用 audit record 構築／lifecycle lock を保持し、file・socket の生成回収と audit の open／append を kernel に委ねる（6-2 の handover と同じ境界）。`EgressBrokerRuntime` は tunnel 予約（reservation）の状態を自分で持ち、accept loop・接続毎の worker thread・worker 回収・失敗通知・停止順序を `SocketBrokerRuntime(concurrency="thread", raw_client=True, deactivate_after_join=True)` に委ねる。`_handle_client(client)` は egress 側に残り、kernel の `open_connection` で `Connection` を開いてから既存の handshake／relay／audit を行う。振る舞い保存は (a) 既存 egress test 不変、(b) kernel 化前の encoder／writer（commit `0ca61c2`）で生成した frame byte と audit 行の golden、で証明する。

**Tech Stack:** Python 3.11、標準 library のみ（`os`, `socket`, `threading`, `time`, `secrets`, `json`, `stat`, `struct`, `dataclasses`）、`unittest`、ruff（`bin/lint`）。

**Spec:** `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`（Phase 6 / stage 1、「乗せ替えの順序とPR分割」の **6-3** 行、「構成」節の設計入力 (1)）。6-4 以降は本 PR の merge 後に別 plan を書く。

## Context

6-1（PR #89）で `broker/frame.py`／`broker/capability.py`、6-2（PR #91）で `broker/audit.py`／`broker/readiness.py`／`broker/runtime.py` を抽出し、handover を乗せ替えた。6-3 は 2 番目の broker として egress を乗せ替える。egress は「接続毎の worker thread」と「tunnel 予約」という broker 固有の追加状態を持ち、これを kernel がどう受けるかを最大の github（6-4）より前に小さい broker で決める、というのが spec の順序理由である。

**egress の現行実装を読んで確認した事実（plan の前提）:**

- protocol（`egress_broker_protocol.py`）は framing／JSON 失敗を `egress metadata …`、schema 失敗を `egress request schema is invalid`／`egress response schema is invalid`、stream 失敗を `egress metadata stream is incomplete` と、**3 種類の接頭辞**で報告する。kernel `FrameSchema` は `label`（schema／encode 用）と `stream_label` の 2 つしか持たないので、frame 用 label の追加が要る（spec 設計入力 (1)）。JSON options は request が `allow_nan=False, separators=(",", ":"), sort_keys=True` を ascii、response が `separators=(",", ":"), sort_keys=True` を ascii（`ensure_ascii`／`allow_nan` は既定）。response には size 上限が無い。
- session（`egress_broker.py`）の `_create_private_file` は capability file を **mode `0400`** で作り、`fchmod` しない。kernel `create_private_file` は `0600` 固定。既存 test `test_creates_private_project_scoped_runtime` が `0400` を固定しているため、kernel に `mode` parameter を追加する。`_open_audit_file`／`_write_audit_record` は handover と label 以外同一（6-2 で確認済み）で `AuditLog(path, label="egress broker audit")` がそのまま賄う。`open_listener`／`close` は handover と同じ形で `bind_private_listener`／`remove_runtime_artifacts` に乗る。
- runtime（`egress_broker_runtime.py`）は accept loop から接続毎に daemon thread `egress-tunnel` を起こし、worker は `OSError` を握り潰し、それ以外の例外で `_error`／`_failed` を立てて `_stop` を set する。`wait_failed(timeout)` は `podman.py`／`agentctl.py` が使う公開 method。`__exit__` は `stop.set → listener close → accept thread join → worker join（deadline は join 後に計算）→ deactivate → close` の順で、**deactivate が worker join の後**に来る（capability が drain 中も生きる）。kernel `stop` は deactivate を最初に呼ぶので、順序を選べる flag が要る。
- 既存 test `tests/container/test_egress_broker_runtime.py` は次を固定している。(a) `runtime._thread` 属性、(b) `runtime._handle_client(client)` を **raw な client socket** を引数に直接呼び、その中で `settimeout`／`SO_PEERCRED`／`makefile` が行われること（`client.credential_calls` を assert）、(c) `mock.patch.object(runtime, "_handle_client", ...)` で差し替えた関数が **serve path から client を引数に呼ばれる**こと（`test_listener_dispatches_connections_concurrently`、`test_client_disconnect_is_graceful_but_worker_bug_is_fatal`、`test_shutdown_allows_inflight_worker_to_finish_final_audit`）、(d) fake client は `close()` を持ち **context manager ではない**。したがって kernel の worker は `_handle_client(client)` を client ごと呼ぶ必要があり、kernel 側で `Connection` を先に開いてしまうと (b) と二重になる。
- container 側 client（`egress_adapter.py`）の `_read_capability` は mode `0400`／`0444` を受理し、owner・size（44 byte）検査を持たず、`O_NONBLOCK` を使わない。既存 test `test_rejects_writable_symlink_or_malformed_capability` が `0600` を **拒否**することを固定している。kernel `read_capability` は `0600` 固定・owner・size 完全一致なので、そのままでは乗らない。`open_gateway_tunnel` も timeout を設定しないので kernel `connect_unix(timeout=...)` に乗らない。
- `podman.py`／`agentctl.py` は `EgressBrokerRuntime.create`、context manager、`wait_failed`、`EgressRuntimeMount(run_dir, project_id, agent)` と `container_name` だけを使う。

**このplanで決めたこと（spec が 6-3 に委ねていた点、および読み込みで新たに判明した点）:**

1. **`FrameSchema` に `frame_label: str | None = None` を追加する（追加のみ）。** `None` なら `label` を使う。framing（`frame is incomplete`／`frame size is invalid`／`frame is invalid`）と JSON（`JSON is invalid`）の message 接頭辞に使い、schema（`schema is invalid`）と encode（`is invalid`／`is too large`）は従来どおり `label`。handover は `frame_label` を渡さないので message 不変。
2. **`create_private_file` に `mode: int = 0o600` を追加する（追加のみ）。** `os.open` の mode と `fchmod` の両方に使う。egress の旧実装は `fchmod` しなかったが、`os.open(..., 0o400)` の結果は umask で bit が落ちる方向にしか変わらず、`fchmod(0o400)` は owner-read 1 bit の存在を保証するだけなので、通常の umask（`022`／`077`）で結果は同一。既存 test は `0400` を assert し、通る。
3. **`SocketBrokerRuntime` に `concurrency="thread"`、`worker_thread_name`、`raw_client`、`deactivate_after_join`、`failed` event と `wait_failed(timeout)`、`workers` の回収を追加する（追加のみ、既定値は 6-2 の挙動）。** thread 方式の worker は `OSError` を接続単位の失敗として握り潰し、それ以外の `BaseException` で `error`／`failed` を立てて `stop_event` を set する。`stop` は accept thread の join 後に worker を `join_timeout` の deadline で join し、残っていれば `did not stop`。`deactivate_after_join=True` のときは deactivate を worker join の後（`did not stop` を raise する前）に呼ぶ。inline（handover）の path は 6-2 と同じ code path を通り、`with client:` のまま。thread 方式の worker は egress の旧実装と同じく `finally: client.close()` で閉じる（egress の fake client は context manager ではない）。
4. **`raw_client=True` を kernel の seam として追加し、egress はこれを使う。** spec は「egress のように socket 本体を要する broker も同じ handler（`Connection`）で受ける」としていたが、上記 (b)(c) のとおり既存 test が `_handle_client(client)` を serve path の単位として固定しており、kernel が先に `Connection` を開くと `SO_PEERCRED`／`makefile` が二重になる。`raw_client=True` では kernel は accept と worker 管理と `client.close()` だけを行い、`handler(client)` を呼ぶ。egress の `_handle_client` は kernel の公開 helper `open_connection(client, *, timeout) -> Connection` で `Connection` を開く（inline path も同じ helper を使うので、peer credential の取り方は 1 実装のまま）。この spec からの逸脱は Task 8 で spec に記録する。
5. **`egress_adapter.py` は 6-3 では触らない。** capability の mode／owner／size 検査の緩さは github の `read_broker_capability`（spec 設計入力 (5)）と同種の差であり、厳格化を受け入れるか kernel を parameter 化するかは 6-4 で github と一緒に決める。Task 8 で spec の `broker/capability.py` 行にこの事実を追記する。
6. **egress の response frame に kernel の size 上限（`MAX_METADATA_BYTES = 16_384`）が付く。** 旧実装は response に上限を持たなかったが、status／code は固定語彙で response は常に 60 byte 未満なので到達不能。handover 6-1 と同じ扱い（comment で明記）。
7. **golden は 1 file `tests/container/test_broker_egress_golden.py` にまとめる。** frame golden 4 test と audit golden 1 test。byte 列は本 plan 作成時に `0ca61c2` の encoder／writer を直接呼んで実測した（Task 1 に転記）。
8. **既存 test の patch 経路。** `tests/container/test_egress_broker.py` は `agent_container.egress_broker.os.chmod` と `agent_container.egress_broker.socket.socket` を patch する。これは `os`／`socket` module そのものへの global patch なので、kernel が `os.chmod(...)`／`socket.socket(...)` を module 属性参照で呼べば kernel 内にも届く（6-2 で確認済みの経路）。`egress_broker.py` は `import os`／`import socket` を **必ず残す**。`tests/container/test_egress_broker_runtime.py` は `threading.Thread` を patch しないが、`mock.patch.object(runtime, "_handle_client", ...)` で instance 属性を差し替えるので、kernel に渡す handler は **`self._handle_client` を bound method で渡さず、呼び出し時に属性参照する lambda** にする。

## Global Constraints

- kernel（`src/agent_container/broker/`）は `agent_container.handover_*` / `egress_*` / `github_*` / `family_*` / `state` を import しない。
- 次の既存 test file は **1 byte も変更しない**: `tests/container/test_egress_broker.py`、`test_egress_broker_protocol.py`、`test_egress_broker_runtime.py`、`test_egress_adapter.py`、`test_egress_gateway.py`、`test_egress_policy.py`、`test_egress_runtime.py`、`test_podman.py`、`test_agentctl*.py`、handover の既存 test 7 file、`test_broker_frame_golden.py`、`test_broker_audit_golden.py`、`tests/integration/test_egress_broker_socket.py`、`tests/integration/test_egress_podman.py`。変更が必要になったら止めて報告する。
- `src/agent_container/egress_adapter.py`、`egress_gateway.py`、`egress_policy.py`、`egress_runtime.py`、`podman.py`、`agentctl.py`、`state.py`、handover の全 module は変更しない。
- egress の例外型と message を保存する。protocol: `ValueError("egress request is invalid")`、`ValueError("egress request is too large")`、`ValueError("egress request schema is invalid")`、`ValueError("egress response is invalid")`、`ValueError("egress response schema is invalid")`、`ValueError("egress metadata frame is incomplete")`、`ValueError("egress metadata frame size is invalid")`、`ValueError("egress metadata JSON is invalid")`、`ValueError("egress metadata stream is incomplete")`。session: `ValueError("egress broker listener state is invalid")`、`ValueError("egress broker socket path is too long")`、`FileExistsError("egress broker socket path already exists")`、`FileExistsError("could not allocate egress broker runtime")`、`RuntimeError("generated egress broker capability has invalid format")`、`OSError("egress broker private file write failed")`、`ValueError("egress broker cleanup failed")`、`ValueError("egress broker session is closed")`、audit: `ValueError("egress broker audit file must be a regular non-symlink file")`、`PermissionError("egress broker audit file must have mode 0600")`、`PermissionError("egress broker audit file must be owned by the current user")`、`ValueError("egress broker audit file changed during validation")`、`OSError("egress broker audit write failed")`。runtime: `EgressBrokerRuntimeError("egress broker failed to start" / "egress broker did not stop" / "egress broker cleanup failed" / "egress broker failed")`、すべて `from None`。
- wire byte: request は `{"capability":…,"domain":…,"operation":…,"port":…,"project_id":…,"sequence":…,"version":…}`（sort_keys、ascii、`allow_nan=False`）、response は `{"code":…,"status":…,"version":…}`（sort_keys、ascii）。4 byte big-endian length header。`PROTOCOL_VERSION = 1`、`MAX_METADATA_BYTES = 16_384`。
- audit 行の serialization は `json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"` を ascii で書く。record の key 順は `timestamp, run, project, agent, operation, status[, stage][, bytes_from_client][, bytes_from_upstream]`。
- `EgressRuntimeMount(run_dir, project_id, agent)` と `container_name`、`EgressBrokerRuntime.create(layout, agent, policy)`、`EgressBrokerRuntime(session)`、`wait_failed(timeout)`、`podman.py` の argv は変えない。
- 作業は `.worktrees/feat-broker-kernel-egress`、branch `feat/broker-kernel-egress`（`main` = `421aa55` から。`0ca61c2` 以降の PR #92／#93 は egress・kernel・spec・CHANGELOG に触れておらず、golden の出自は `0ca61c2` のまま正しい）。worktree 作成後に `chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json`。
- test: `cd <worktree> && PYTHONPATH=src python3 -m unittest <module> -v`。lint: `bin/lint`。
- commit 末尾: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 既存 broker の bug を見つけても直さない（Issue 化して別 PR）。

---

## File Structure

| path | 役割 |
| --- | --- |
| `docs/superpowers/plans/2026-09-05-broker-kernel-6-3-egress.md` | **Create.** 本 plan の写し |
| `tests/container/test_broker_egress_golden.py` | **Create.** kernel 化前（`0ca61c2`）の encoder／writer で生成した frame byte と audit 行を固定 |
| `src/agent_container/broker/frame.py` | **Modify.** `FrameSchema.frame_label`（既定 `None` → `label`）と `frame_prefix` property。framing／JSON の message だけ接頭辞を切り替える |
| `tests/container/test_broker_frame.py` | **Modify.** `frame_label` の unit test を追加（既存 test は変えない） |
| `src/agent_container/egress_broker_protocol.py` | **Modify.** codec／stream 読みを kernel `frame` に委ねる。field 検証（`_validate_request`／`_validate_response`）と公開名は維持 |
| `src/agent_container/broker/runtime.py` | **Modify.** `create_private_file(mode=0o600)`、`open_connection`、`SocketBrokerRuntime` の `concurrency`／`worker_thread_name`／`raw_client`／`deactivate_after_join`／`failed`／`workers`／`wait_failed` |
| `tests/container/test_broker_runtime.py` | **Modify.** `mode`、`open_connection`、thread 方式、raw client、deactivate 順序、`wait_failed` の unit test を追加（既存 test は変えない。`FakeClient` に `close()` を、`make_runtime` に `**options` を足す） |
| `src/agent_container/egress_broker.py` | **Modify.** `_create_private_file`／`_open_audit_file`／`_write_audit_record`／capability 生成／run dir 割り当て／`open_listener` 本体／`close` の artifact 回収を kernel へ委ねる。`import os`／`import socket` は維持 |
| `src/agent_container/egress_broker_runtime.py` | **Modify.** `SocketBrokerRuntime` を合成。tunnel 予約と `_handle_client` は残す。`_thread` は property、`wait_failed` は委譲 |
| `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` | **Modify.** 6-3 の決定（`raw_client` seam、adapter 据え置き、`mode`、`frame_label`、response 上限）を記録し、`frame.py`／`runtime.py`／`capability.py` 行と PR 表の 6-3 行を実装に合わせる |
| `CHANGELOG.md` | **Modify.** Unreleased / Added に 1 行 |

## Interfaces（全 task 共通の contract）

`src/agent_container/broker/frame.py`（Task 2 で追加、Task 3 で使う）:

```python
@dataclass(frozen=True)
class FrameSchema:
    label: str
    stream_label: str
    fields: frozenset[str]
    max_bytes: int
    json: JsonOptions
    frame_label: str | None = None     # 追加。None なら label

    @property
    def frame_prefix(self) -> str      # frame_label if not None else label
```

message の対応（`decode_frame`／`read_frame`／`encode_frame`）:

| message | 接頭辞 |
| --- | --- |
| `… frame is incomplete`、`… frame size is invalid`、`… frame is invalid`、`… JSON is invalid` | `frame_prefix` |
| `… schema is invalid`、`… is invalid`（encode）、`… is too large` | `label` |
| `… is incomplete`、`… is invalid`（`read_exact`）、`… write failed`（`write_all`） | `stream_label`（従来どおり） |

`src/agent_container/broker/runtime.py`（Task 4／Task 6 で追加、Task 5／Task 7 で使う）:

```python
def create_private_file(path: Path, body: str, *, label: str, mode: int = 0o600) -> None
    # os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, mode) → os.fchmod(descriptor, mode) → ascii write loop → fsync → close

def open_connection(client: Any, *, timeout: float) -> Connection
    # client.settimeout(timeout) → client.getsockopt(SOL_SOCKET, SO_PEERCRED, 12) → struct.unpack("3i") の uid →
    # Connection(client, client.makefile("rwb", buffering=0), peer_uid)。inline path と egress の _handle_client が共用する

@dataclass
class SocketBrokerRuntime:
    label: str
    thread_name: str
    open_listener: Callable[[int], Any]
    handler: Callable[[Any], object]      # raw_client=False: Connection を受ける。True: accept した client socket を受ける
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = AlwaysReady()
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    concurrency: str = "inline"           # 追加。"inline" | "thread"。それ以外は __post_init__ で ValueError(f"{label} concurrency mode is invalid")
    worker_thread_name: str = ""          # 追加。thread 方式の worker 名。"" なら f"{thread_name}-worker"
    raw_client: bool = False              # 追加。True なら Connection を開かず handler(client) を呼ぶ
    deactivate_after_join: bool = False   # 追加。True なら deactivate を accept thread と worker の join 後に呼ぶ
    stop_event: threading.Event           # init=False
    failed: threading.Event               # 追加、init=False。accept loop または worker の失敗で set
    thread: Any | None                    # init=False
    listener: Any | None                  # init=False
    error: BaseException | None           # init=False
    exited: bool                          # init=False
    workers: set[Any]                     # 追加、init=False。生存中の worker thread
    worker_lock: threading.Lock           # 追加、init=False

    def start(self) -> None               # 6-2 と同じ
    def wait_failed(self, timeout: float) -> bool   # 追加。failed.wait(timeout)
    def stop(self, *, join_timeout: float) -> None
        # exited なら return。stop_event.set。deactivate_after_join=False なら deactivate。listener close（OSError → cleanup_failed）。
        # accept thread join(join_timeout) → did_not_stop。worker を deadline = monotonic + join_timeout で順に join、残れば did_not_stop。
        # deactivate_after_join=True ならここで deactivate。did_not_stop なら error_type(f"{label} did not stop")（close せず再試行可能）。
        # close（OSError/ValueError → cleanup_failed、成功で exited=True）。cleanup_failed → f"{label} cleanup failed"。error → f"{label} failed"。
```

accept path の挙動:

| mode | 接続毎の処理 |
| --- | --- |
| `inline`（既定、handover） | accept thread 上で `with client:` → `raw_client` なら `handler(client)`、そうでなければ `open_connection` → `handler(connection)` → `finally: connection.stream.close()`。handler の例外は accept loop を止め `error`／`failed` を立てる（6-2 と同じ） |
| `thread`（egress） | accept 毎に daemon thread（`worker_thread_name`）を起こし `workers` に登録。worker は上と同じ処理を `try` で包み、`OSError` は握り潰す、それ以外の `BaseException` は `error`／`failed` を立てて `stop_event.set()`。`finally: client.close()` と `workers` からの除去。thread の `start()` 失敗は `workers` から外し `client.close()` して accept loop へ再送出 |

`src/agent_container/egress_broker.py`（Task 5）: 公開名（`MANAGED_EGRESS_DOMAINS`、`EgressAuthorizationError`、`EgressBrokerSession` と全 method／field）は不変。

`src/agent_container/egress_broker_runtime.py`（Task 7）: 公開名（`EgressBrokerRuntimeError`、`EgressRuntimeMount`、`EgressBrokerRuntime.create`／`wait_failed`／`__enter__`／`__exit__`）と test が触る private（`_thread`、`_handle_client`、`_reserve_tunnel`、`_mark_tunnel_created`、`_release_tunnel`、`_created_tunnels`、`_active_reservations`）は不変。

---

### Task 1: worktree 準備と egress golden（kernel 化前の encoder／writer で固定）

**Files:**
- Create: `docs/superpowers/plans/2026-09-05-broker-kernel-6-3-egress.md`
- Create: `tests/container/test_broker_egress_golden.py`

**Interfaces:**
- Consumes: 現行の `egress_broker_protocol`／`EgressBrokerSession`（commit `0ca61c2`）
- Produces: request 1 frame、response 3 frame、audit 3 行の golden byte（後続 task の回帰網）

- [ ] **Step 1: worktree を作る**

```bash
cd /workspace
git status --short          # 追跡 file の変更が無いこと（launcher が置く dotfile の untracked は無視してよい）
git worktree add .worktrees/feat-broker-kernel-egress -b feat/broker-kernel-egress main
cd .worktrees/feat-broker-kernel-egress
chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json
git rev-parse --short HEAD  # 421aa55 であること（golden の出自 0ca61c2 と egress／kernel の file は同一。git diff --quiet 0ca61c2 421aa55 -- src/agent_container/broker src/agent_container/egress_*.py で確認できる）
```

- [ ] **Step 2: plan を repo に写す**

本 plan を `docs/superpowers/plans/2026-09-05-broker-kernel-6-3-egress.md` として保存する（main の workspace に既に同 path で保存済みなら `cp` でよい）。

- [ ] **Step 3: golden test を書く（現行 code で PASS する test）**

`tests/container/test_broker_egress_golden.py`:

```python
"""Golden bytes captured from the pre-kernel egress broker.

Generated at commit 0ca61c2 with agent_container.egress_broker_protocol and
EgressBrokerSession.audit before the egress broker used the broker kernel.
Never regenerate these bytes from the kernel's own output; a mismatch here
means the wire format or the audit line format changed.
"""

from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import decode_request_frame
from agent_container.egress_broker_protocol import decode_response_frame
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_broker_protocol import encode_response_frame
from agent_container.egress_broker_protocol import read_request_frame
from agent_container.egress_broker_protocol import read_response_frame
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


GOLDEN_REQUEST = EgressRequest(
    1, "A" * 43, "agent-container", 1, "connect", "api.example.com", 443
)
GOLDEN_REQUEST_BYTES = (
    b'\x00\x00\x00\xb0{"capability":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
    b'"domain":"api.example.com","operation":"connect","port":443,'
    b'"project_id":"agent-container","sequence":1,"version":1}'
)
GOLDEN_RESPONSE_OK = EgressResponse(1, "ok", "connect")
GOLDEN_RESPONSE_OK_BYTES = b'\x00\x00\x00,{"code":"connect","status":"ok","version":1}'
GOLDEN_RESPONSE_DENIED = EgressResponse(1, "denied", "authentication")
GOLDEN_RESPONSE_DENIED_BYTES = (
    b'\x00\x00\x007{"code":"authentication","status":"denied","version":1}'
)
GOLDEN_RESPONSE_ERROR = EgressResponse(1, "error", "connect")
GOLDEN_RESPONSE_ERROR_BYTES = (
    b'\x00\x00\x00/{"code":"connect","status":"error","version":1}'
)

GOLDEN_RUN_LABEL = "0123456789abcdef"
GOLDEN_AUDIT_BYTES = (
    b'{"timestamp":"2026-09-04T00:00:00.000001+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"ok","bytes_from_client":12,"bytes_from_upstream":34}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000002+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"denied","stage":"policy"}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000003+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"error","stage":"relay"}\n'
)


class EgressGoldenFrameTest(unittest.TestCase):
    def test_request_encodes_to_the_pre_kernel_bytes(self) -> None:
        self.assertEqual(encode_request_frame(GOLDEN_REQUEST), GOLDEN_REQUEST_BYTES)

    def test_request_bytes_decode_to_the_same_value(self) -> None:
        self.assertEqual(
            decode_request_frame(GOLDEN_REQUEST_BYTES),
            (GOLDEN_REQUEST, len(GOLDEN_REQUEST_BYTES)),
        )
        self.assertEqual(read_request_frame(BytesIO(GOLDEN_REQUEST_BYTES)), GOLDEN_REQUEST)

    def test_responses_encode_to_the_pre_kernel_bytes(self) -> None:
        self.assertEqual(encode_response_frame(GOLDEN_RESPONSE_OK), GOLDEN_RESPONSE_OK_BYTES)
        self.assertEqual(
            encode_response_frame(GOLDEN_RESPONSE_DENIED), GOLDEN_RESPONSE_DENIED_BYTES
        )
        self.assertEqual(
            encode_response_frame(GOLDEN_RESPONSE_ERROR), GOLDEN_RESPONSE_ERROR_BYTES
        )

    def test_response_bytes_decode_to_the_same_values(self) -> None:
        self.assertEqual(
            decode_response_frame(GOLDEN_RESPONSE_OK_BYTES),
            (GOLDEN_RESPONSE_OK, len(GOLDEN_RESPONSE_OK_BYTES)),
        )
        self.assertEqual(
            read_response_frame(BytesIO(GOLDEN_RESPONSE_DENIED_BYTES)),
            GOLDEN_RESPONSE_DENIED,
        )
        self.assertEqual(
            decode_response_frame(GOLDEN_RESPONSE_ERROR_BYTES),
            (GOLDEN_RESPONSE_ERROR, len(GOLDEN_RESPONSE_ERROR_BYTES)),
        )


class EgressAuditGoldenTest(unittest.TestCase):
    def test_audit_lines_match_the_pre_kernel_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="egress-golden-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            layout = StateLayout(state.resolve(), "agent-container")
            policy = EgressPolicy(1, "allowlist", ("api.example.com",))
            session = EgressBrokerSession.create(layout, "codex", policy)
            try:
                stamps = iter(
                    [
                        "2026-09-04T00:00:00.000001+00:00",
                        "2026-09-04T00:00:00.000002+00:00",
                        "2026-09-04T00:00:00.000003+00:00",
                    ]
                )
                fixed_now = mock.Mock()
                fixed_now.isoformat.side_effect = lambda: next(stamps)
                with mock.patch(
                    "agent_container.egress_broker.datetime"
                ) as fixed_datetime, mock.patch.object(
                    EgressBrokerSession,
                    "run_label",
                    new_callable=mock.PropertyMock,
                    return_value=GOLDEN_RUN_LABEL,
                ):
                    fixed_datetime.now.return_value = fixed_now
                    session.audit("ok", bytes_from_client=12, bytes_from_upstream=34)
                    session.audit("denied", stage="policy")
                    session.audit("error", stage="relay")

                self.assertEqual(session.audit_file.read_bytes(), GOLDEN_AUDIT_BYTES)
                self.assertEqual(
                    stat.S_IMODE(session.audit_file.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(session.capability_path.stat().st_mode), 0o400
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: 現行 code で PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_egress_golden -v`
Expected: 5 tests, OK。（byte 列は `0ca61c2` の `encode_request_frame`／`encode_response_frame`／`EgressBrokerSession.audit` を直接呼んで 2026-09-05 に実測したもの。`timestamp` は `agent_container.egress_broker.datetime` を patch、`run` は `run_label` property を patch して固定）

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-09-05-broker-kernel-6-3-egress.md tests/container/test_broker_egress_golden.py
git commit -m "test: pin egress wire bytes and audit lines before moving egress onto the broker kernel

Golden bytes generated by the pre-kernel egress encoder and audit writer at
0ca61c2. The kernel migration that follows must reproduce them exactly.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `FrameSchema.frame_label`（TDD）

**Files:**
- Modify: `src/agent_container/broker/frame.py`
- Modify: `tests/container/test_broker_frame.py`（末尾に class を追加）

**Interfaces:**
- Consumes: 6-1 の `FrameSchema`、`decode_frame`、`read_frame`、`encode_frame`
- Produces: `FrameSchema(..., frame_label=...)` と `FrameSchema.frame_prefix`（Task 3 が使う）

- [ ] **Step 1: 失敗する test を書く**

`tests/container/test_broker_frame.py` の末尾（`if __name__ == "__main__":` の前）に追加:

```python
class FrameLabelTest(unittest.TestCase):
    def test_frame_label_defaults_to_label(self) -> None:
        self.assertIsNone(SCHEMA.frame_label)
        self.assertEqual(SCHEMA.frame_prefix, "test request")
        with self.assertRaises(ValueError) as raised:
            decode_frame(SCHEMA, frame(b"not-json"))
        self.assertEqual(str(raised.exception), "test request JSON is invalid")

    def test_frame_label_prefixes_framing_and_json_errors_only(self) -> None:
        schema = FrameSchema(
            label="test request",
            stream_label="test stream",
            fields=frozenset({"version", "name"}),
            max_bytes=64,
            json=COMPACT,
            frame_label="test metadata",
        )
        self.assertEqual(schema.frame_prefix, "test metadata")
        cases = (
            (b"\x00\x00\x00", "test metadata frame is incomplete"),
            (struct.pack(">I", 0), "test metadata frame size is invalid"),
            (struct.pack(">I", 5) + b"{}", "test metadata frame is incomplete"),
            (frame(b"not-json"), "test metadata JSON is invalid"),
            (frame(b'{"version":1,"version":1}'), "test metadata JSON is invalid"),
            (frame(b'{"version":1}'), "test request schema is invalid"),
        )
        for data, message in cases:
            with self.subTest(message=message), self.assertRaises(ValueError) as raised:
                decode_frame(schema, data)
            self.assertEqual(str(raised.exception), message)

        with self.assertRaises(ValueError) as raised:
            read_frame(schema, BytesIO(struct.pack(">I", 0)))
        self.assertEqual(str(raised.exception), "test metadata frame size is invalid")
        with self.assertRaises(ValueError) as raised:
            read_frame(schema, BytesIO(b"\x00\x00"))
        self.assertEqual(str(raised.exception), "test stream is incomplete")
        with self.assertRaises(ValueError) as raised:
            encode_frame(schema, {"version": 1, "name": "x" * 64})
        self.assertEqual(str(raised.exception), "test request is too large")
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame.FrameLabelTest -v`
Expected: 2 tests FAIL／ERROR（`frame_label` は unexpected keyword、`frame_prefix` は AttributeError）。

- [ ] **Step 3: 実装**

`src/agent_container/broker/frame.py` の `FrameSchema` を次に置き換える:

```python
@dataclass(frozen=True)
class FrameSchema:
    label: str
    stream_label: str
    fields: frozenset[str]
    max_bytes: int
    json: JsonOptions
    frame_label: str | None = None

    @property
    def frame_prefix(self) -> str:
        return self.label if self.frame_label is None else self.frame_label
```

`decode_frame` の 4 か所を置き換える（`schema is invalid` は `schema.label` のまま）:

```python
def decode_frame(schema: FrameSchema, data: bytes) -> tuple[dict[str, Any], int]:
    if not isinstance(data, bytes) or len(data) < HEADER_BYTES:
        raise ValueError(f"{schema.frame_prefix} frame is incomplete")
    length = struct.unpack(">I", data[:HEADER_BYTES])[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.frame_prefix} frame size is invalid")
    consumed = HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError(f"{schema.frame_prefix} frame is incomplete")
    try:
        text = data[HEADER_BYTES:consumed].decode(schema.json.encoding)
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"{schema.frame_prefix} JSON is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != schema.fields:
        raise ValueError(f"{schema.label} schema is invalid")
    return decoded, consumed
```

`read_frame` の 2 か所を置き換える:

```python
def read_frame(schema: FrameSchema, stream: BinaryIO) -> dict[str, Any]:
    header = read_exact(stream, HEADER_BYTES, label=schema.stream_label)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.frame_prefix} frame size is invalid")
    body = read_exact(stream, length, label=schema.stream_label)
    decoded, consumed = decode_frame(schema, header + body)
    if consumed != len(header) + len(body):
        raise ValueError(f"{schema.frame_prefix} frame is invalid")
    return decoded
```

`encode_frame`、`read_exact`、`write_all` は変えない。

- [ ] **Step 4: PASS と回帰を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame tests.container.test_broker_frame_golden tests.container.test_handover_broker_protocol tests.container.test_handover_broker_client -v 2>&1 | tail -3`
Expected: OK（handover は `frame_label` を渡さないので message 不変）。

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/frame.py tests/container/test_broker_frame.py
git commit -m "feat: let a frame schema prefix framing errors separately from schema errors

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `egress_broker_protocol.py` を kernel `frame` に乗せ替える

**Files:**
- Modify: `src/agent_container/egress_broker_protocol.py`
- Test: `tests/container/test_egress_broker_protocol.py`（不変）、`tests/container/test_broker_egress_golden.py`（不変）

**Interfaces:**
- Consumes: `FrameSchema(label, stream_label, fields, max_bytes, json, frame_label)`、`JsonOptions`、`encode_frame`、`decode_frame`、`read_frame`
- Produces: 公開名不変（`PROTOCOL_VERSION`、`MAX_METADATA_BYTES`、`MAX_SEQUENCE`、`EgressRequest`、`EgressResponse`、`encode_request_frame`、`decode_request_frame`、`encode_response_frame`、`decode_response_frame`、`read_request_frame`、`read_response_frame`）

- [ ] **Step 1: 乗せ替え前の test が PASS していることを確認（baseline）**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker_protocol tests.container.test_broker_egress_golden -v 2>&1 | tail -3`
Expected: 10 tests OK。

- [ ] **Step 2: module を書き換える**

`src/agent_container/egress_broker_protocol.py` 全体を次に置き換える:

```python
from dataclasses import dataclass
from typing import Any
from typing import BinaryIO

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import read_frame
from agent_container.egress_policy import validate_domain


PROTOCOL_VERSION = 1
MAX_METADATA_BYTES = 16_384
MAX_SEQUENCE = (1 << 63) - 1
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "capability",
        "project_id",
        "sequence",
        "operation",
        "domain",
        "port",
    }
)
_RESPONSE_FIELDS = frozenset({"version", "status", "code"})
_RESPONSE_STATUSES = frozenset({"ok", "denied", "error"})
_RESPONSE_CODES = frozenset(
    {
        "authentication",
        "policy",
        "resolve",
        "connect",
        "limit",
        "relay",
        "unavailable",
    }
)
_FRAME_LABEL = "egress metadata"
_STREAM_LABEL = "egress metadata stream"
_REQUEST_SCHEMA = FrameSchema(
    label="egress request",
    stream_label=_STREAM_LABEL,
    fields=_REQUEST_FIELDS,
    max_bytes=MAX_METADATA_BYTES,
    json=JsonOptions(allow_nan=False, sort_keys=True, separators=(",", ":"), encoding="ascii"),
    frame_label=_FRAME_LABEL,
)
# The pre-kernel encoder put no byte cap on responses; the fixed status/code
# vocabulary keeps every response far below the request cap reused here.
_RESPONSE_SCHEMA = FrameSchema(
    label="egress response",
    stream_label=_STREAM_LABEL,
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_METADATA_BYTES,
    json=JsonOptions(sort_keys=True, separators=(",", ":"), encoding="ascii"),
    frame_label=_FRAME_LABEL,
)


@dataclass(frozen=True)
class EgressRequest:
    version: int
    capability: str
    project_id: str
    sequence: int
    operation: str
    domain: str
    port: int


@dataclass(frozen=True)
class EgressResponse:
    version: int
    status: str
    code: str


def _validate_request(request: EgressRequest) -> None:
    if (
        isinstance(request.version, bool)
        or not isinstance(request.version, int)
        or request.version != PROTOCOL_VERSION
    ):
        raise ValueError("egress request schema is invalid")
    if (
        not isinstance(request.capability, str)
        or not request.capability
        or not isinstance(request.project_id, str)
        or not request.project_id
    ):
        raise ValueError("egress request schema is invalid")
    if (
        isinstance(request.sequence, bool)
        or not isinstance(request.sequence, int)
        or not 1 <= request.sequence <= MAX_SEQUENCE
    ):
        raise ValueError("egress request schema is invalid")
    if request.operation != "connect":
        raise ValueError("egress request schema is invalid")
    validate_domain(request.domain)
    if (
        isinstance(request.port, bool)
        or not isinstance(request.port, int)
        or request.port != 443
    ):
        raise ValueError("egress request schema is invalid")


def _request_from_values(values: dict[str, Any]) -> EgressRequest:
    request = EgressRequest(**values)
    _validate_request(request)
    return request


def encode_request_frame(request: EgressRequest) -> bytes:
    if not isinstance(request, EgressRequest):
        raise ValueError("egress request is invalid")
    _validate_request(request)
    return encode_frame(
        _REQUEST_SCHEMA,
        {
            "version": request.version,
            "capability": request.capability,
            "project_id": request.project_id,
            "sequence": request.sequence,
            "operation": request.operation,
            "domain": request.domain,
            "port": request.port,
        },
    )


def decode_request_frame(data: bytes) -> tuple[EgressRequest, int]:
    decoded, consumed = decode_frame(_REQUEST_SCHEMA, data)
    return _request_from_values(decoded), consumed


def _validate_response(response: EgressResponse) -> None:
    if (
        isinstance(response.version, bool)
        or not isinstance(response.version, int)
        or response.version != PROTOCOL_VERSION
        or not isinstance(response.status, str)
        or response.status not in _RESPONSE_STATUSES
        or not isinstance(response.code, str)
        or response.code not in _RESPONSE_CODES
    ):
        raise ValueError("egress response schema is invalid")


def _response_from_values(values: dict[str, Any]) -> EgressResponse:
    response = EgressResponse(**values)
    _validate_response(response)
    return response


def encode_response_frame(response: EgressResponse) -> bytes:
    if not isinstance(response, EgressResponse):
        raise ValueError("egress response is invalid")
    _validate_response(response)
    return encode_frame(
        _RESPONSE_SCHEMA,
        {
            "version": response.version,
            "status": response.status,
            "code": response.code,
        },
    )


def decode_response_frame(data: bytes) -> tuple[EgressResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    return _response_from_values(decoded), consumed


def read_request_frame(stream: BinaryIO) -> EgressRequest:
    return _request_from_values(read_frame(_REQUEST_SCHEMA, stream))


def read_response_frame(stream: BinaryIO) -> EgressResponse:
    return _response_from_values(read_frame(_RESPONSE_SCHEMA, stream))
```

削除されるもの: `import json`、`import struct`、`_HEADER_BYTES`、`_reject_constant`、`_object_without_duplicates`、`_decode_json`、`_frame_body`、`_read_exact`、`_read_frame`（`src`／`tests` からの private import は無いことを 2026-09-05 に `grep -rn "egress_broker_protocol import _" src tests` で確認済み）。

message の対応を確認する: framing／JSON の 4 種は `frame_label="egress metadata"` で `egress metadata frame is incomplete`／`egress metadata frame size is invalid`／`egress metadata JSON is invalid`、schema は `label` で `egress request schema is invalid`／`egress response schema is invalid`、size は `egress request is too large`、stream は `stream_label` で `egress metadata stream is incomplete`。旧 `_read_exact` は `stream.read` の `OSError` を素通しし、kernel `read_exact` は `ValueError(f"{stream_label} is invalid")` に変換するが、両方の呼び出し側（runtime の `_handle_client` と adapter の `open_gateway_tunnel`）は `(OSError, ValueError)` を同じ枝で受けるので観測できる挙動は変わらない。

- [ ] **Step 3: 既存 test と golden が不変のまま PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker_protocol tests.container.test_broker_egress_golden tests.container.test_egress_adapter tests.container.test_egress_broker_runtime -v 2>&1 | tail -3`
Expected: OK。`git status --short tests/` に egress の既存 test が現れない。

- [ ] **Step 4: lint**

Run: `bin/lint`
Expected: pass（未使用 import が残っていれば F401 で落ちる）。

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/egress_broker_protocol.py
git commit -m "refactor: frame the egress protocol with the broker kernel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `create_private_file` の `mode` parameter（TDD）

**Files:**
- Modify: `src/agent_container/broker/runtime.py:22-40`
- Modify: `tests/container/test_broker_runtime.py`（`CreatePrivateFileTest` に 1 test 追加）

**Interfaces:**
- Produces: `create_private_file(path, body, *, label, mode=0o600)`（Task 5 が `mode=0o400` で使う）

- [ ] **Step 1: 失敗する test を書く**

`tests/container/test_broker_runtime.py` の `CreatePrivateFileTest` に追加:

```python
    def test_mode_parameter_creates_an_owner_read_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            create_private_file(path, "abc\n", label=LABEL, mode=0o400)
            self.assertEqual(path.read_bytes(), b"abc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
            default = Path(directory) / "default"
            create_private_file(default, "abc\n", label=LABEL)
            self.assertEqual(stat.S_IMODE(default.stat().st_mode), 0o600)
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime.CreatePrivateFileTest -v`
Expected: 新 test が `TypeError: ... unexpected keyword argument 'mode'` で ERROR。

- [ ] **Step 3: 実装**

`src/agent_container/broker/runtime.py` の `create_private_file` を次に置き換える:

```python
def create_private_file(
    path: Path, body: str, *, label: str, mode: int = 0o600
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        encoded = body.encode("ascii")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError(f"{label} private file write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
```

- [ ] **Step 4: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime.CreatePrivateFileTest tests.container.test_handover_broker -v 2>&1 | tail -3`
Expected: OK。

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/runtime.py tests/container/test_broker_runtime.py
git commit -m "feat: let a broker choose the private file mode of its capability

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `EgressBrokerSession` を `AuditLog` と資源 helper に乗せ替える

**Files:**
- Modify: `src/agent_container/egress_broker.py`
- Test: `tests/container/test_egress_broker.py`（不変）、`tests/container/test_broker_egress_golden.py`（不変）

**Interfaces:**
- Consumes: `AuditLog(path, *, label)`／`validate()`／`append(record)`、`allocate_run_dir(project_root, *, label)`、`generate_capability(*, label)`、`create_private_file(path, body, *, label, mode)`、`bind_private_listener(socket_path, *, backlog, label)`、`remove_runtime_artifacts(*, capability_path, socket_path, run_dir) -> bool`
- Produces: `EgressBrokerSession` の公開 API 不変（Task 7 が `open_listener`／`authorize`／`audit`／`deactivate`／`close`／`run_dir`／`project_id`／`agent` を使う）

- [ ] **Step 1: baseline**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_broker_egress_golden -v 2>&1 | tail -3`
Expected: OK。

- [ ] **Step 2: module を書き換える**

`src/agent_container/egress_broker.py` 全体を次に置き換える（`authorize`、`audit` の record 構築、`EgressAuthorizationError`、`MANAGED_EGRESS_DOMAINS` は旧実装のまま）:

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import socket
import threading

from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import PROTOCOL_VERSION
from agent_container.egress_policy import EgressPolicy
from agent_container.state import ensure_private_directory
from agent_container.state import StateLayout
from agent_container.state import validate_agent


MANAGED_EGRESS_DOMAINS: dict[str, frozenset[str]] = {
    "codex": frozenset(),
    "claude": frozenset(),
}
_LABEL = "egress broker"
_AUDIT_LABEL = "egress broker audit"
# The container-side adapter reads the capability through a read-only bind
# mount and rejects writable files, so the file is created owner-read-only.
_CAPABILITY_FILE_MODE = 0o400
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_AUDIT_STAGES = frozenset(
    {"authentication", "policy", "resolve", "connect", "limit", "relay", "unavailable"}
)
_MAX_TUNNEL_BYTES = 1 << 31


class EgressAuthorizationError(ValueError):
    def __init__(self, stage: str) -> None:
        if stage not in {"authentication", "policy"}:
            raise ValueError("egress authorization stage is invalid")
        self.stage = stage
        super().__init__("egress broker request is not allowed")


@dataclass
class EgressBrokerSession:
    project_id: str
    agent: str
    owner_uid: int
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _allowed_domains: frozenset[str] = field(repr=False)
    _capability: str = field(repr=False)
    _expected_sequence: int = field(default=1, repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @classmethod
    def create(
        cls, layout: StateLayout, agent: str, policy: EgressPolicy
    ) -> "EgressBrokerSession":
        ensure_private_directory(layout.root)
        selected_agent = validate_agent(agent)
        if policy.version != 1 or policy.mode != "allowlist":
            raise ValueError("egress policy is invalid")
        allowed = frozenset(policy.additional_domains) | MANAGED_EGRESS_DOMAINS[
            selected_agent
        ]
        ensure_private_directory(layout.egress_broker_root, create=True)
        ensure_private_directory(layout.egress_broker_root / "audit", create=True)
        ensure_private_directory(layout.egress_broker_root / "r", create=True)
        project_root = ensure_private_directory(
            layout.egress_broker_run_root, create=True
        )
        audit_file = layout.egress_broker_audit_file
        AuditLog(audit_file, label=_AUDIT_LABEL).validate()

        run_id, run_dir = allocate_run_dir(project_root, label=_LABEL)
        try:
            capability = generate_capability(label=_LABEL)
        except RuntimeError:
            shutil.rmtree(run_dir)
            raise
        capability_path = run_dir / "capability"
        try:
            create_private_file(
                capability_path,
                capability + "\n",
                label=_LABEL,
                mode=_CAPABILITY_FILE_MODE,
            )
        except Exception:
            shutil.rmtree(run_dir)
            raise
        return cls(
            project_id=layout.project_id,
            agent=selected_agent,
            owner_uid=os.getuid(),
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _allowed_domains=allowed,
            _capability=capability,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(self, request: EgressRequest, peer_uid: int) -> str:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("egress broker session is closed")
            if peer_uid != self.owner_uid:
                raise ValueError("egress broker request is not authorized")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("egress broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("egress broker request is not authorized")
            if request.project_id != self.project_id:
                raise ValueError("egress broker request project is not allowed")
            if request.sequence != self._expected_sequence:
                raise ValueError("egress broker request sequence is invalid")
            if request.operation != "connect" or request.port != 443:
                raise EgressAuthorizationError("policy")
            if request.domain not in self._allowed_domains:
                raise EgressAuthorizationError("policy")
            self._expected_sequence += 1
            return request.domain

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("egress broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        self._listener = listener
        return listener

    def audit(
        self,
        status: str,
        *,
        stage: str | None = None,
        bytes_from_client: int | None = None,
        bytes_from_upstream: int | None = None,
    ) -> None:
        if self._closed:
            raise ValueError("egress broker session is closed")
        if status not in _AUDIT_STATUSES:
            raise ValueError("egress broker audit status is invalid")
        if status == "ok":
            if stage is not None:
                raise ValueError("egress broker audit stage is invalid")
        elif stage not in _AUDIT_STAGES:
            raise ValueError("egress broker audit stage is invalid")
        counts = {
            "bytes_from_client": bytes_from_client,
            "bytes_from_upstream": bytes_from_upstream,
        }
        for value in counts.values():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_TUNNEL_BYTES
            ):
                raise ValueError("egress broker audit byte count is invalid")
        record: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_label,
            "project": self.project_id,
            "agent": self.agent,
            "operation": "connect",
            "status": status,
        }
        if stage is not None:
            record["stage"] = stage
        for key, value in counts.items():
            if value is not None:
                record[key] = value
        AuditLog(self.audit_file, label=_AUDIT_LABEL).append(record)

    def close(self) -> None:
        if self._cleanup_complete:
            return
        self.deactivate()
        cleanup_failed = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self._listener = None
        if remove_runtime_artifacts(
            capability_path=self.capability_path,
            socket_path=self.socket_path,
            run_dir=self.run_dir,
        ):
            cleanup_failed = True
        if cleanup_failed:
            raise ValueError("egress broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "EgressBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
```

削除されるもの: `import json`、`import re`、`import stat`、`_CAPABILITY`、`_MAX_UNIX_SOCKET_PATH_BYTES`、`_NOFOLLOW`、`_NONBLOCK`、`_create_private_file`、`_open_audit_file`、`_write_audit_record`。`import os` と `import socket` は既存 test の patch 経路（`agent_container.egress_broker.os.chmod`／`.socket.socket`）に必要なので残す（`os.getuid` と型注釈で実際にも使う）。

- [ ] **Step 3: 既存 test と golden が不変のまま PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_broker_egress_golden tests.container.test_egress_broker_runtime -v 2>&1 | tail -3`
Expected: OK。特に `test_creates_private_project_scoped_runtime`（capability `0400`）、`test_listener_is_single_use_and_close_removes_exact_runtime`（global patch した `socket.socket`／`os.chmod` が kernel 内で使われる）、`test_rejects_overlong_socket_path_before_socket_creation`（`socket.socket` 生成前に `too long`）、`test_close_refuses_replaced_capability_path`、`test_create_rejects_symlinked_or_broad_audit_file`（`regular non-symlink`／`mode 0600`）が通ること。

- [ ] **Step 4: lint**

Run: `bin/lint`
Expected: pass。

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/egress_broker.py
git commit -m "refactor: back the egress session with the broker kernel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `SocketBrokerRuntime` の thread 方式・raw client・deactivate 順序・`wait_failed`、`open_connection`（TDD）

**Files:**
- Modify: `src/agent_container/broker/runtime.py:120-248`
- Modify: `tests/container/test_broker_runtime.py`（`FakeClient` に `close()`、`make_runtime` に `**options`、新 class を追加。既存 test は変えない）

**Interfaces:**
- Consumes: 6-2 の `Connection`、`SocketBrokerRuntime`
- Produces: `open_connection(client, *, timeout) -> Connection`、`SocketBrokerRuntime(..., concurrency=, worker_thread_name=, raw_client=, deactivate_after_join=)`、`wait_failed(timeout) -> bool`、属性 `failed`／`workers`（Task 7 が使う）

- [ ] **Step 1: test の fake と helper を拡張する**

`tests/container/test_broker_runtime.py` の `FakeClient` に `close()` を追加（`__enter__`／`__exit__` はそのまま）:

```python
    def close(self) -> None:
        self.closed = True
```

`make_runtime` を次に置き換える（既存の呼び出しは全て互換）:

```python
def make_runtime(
    listener: FakeListener,
    handler=None,
    *,
    readiness=None,
    open_listener=None,
    close=None,
    **options,
) -> tuple[SocketBrokerRuntime, dict[str, int]]:
    calls = {"open": 0, "deactivate": 0, "close": 0, "backlog": 0}

    def default_open(backlog: int) -> FakeListener:
        calls["open"] += 1
        calls["backlog"] = backlog
        return listener

    def deactivate() -> None:
        calls["deactivate"] += 1

    def default_close() -> None:
        calls["close"] += 1

    if readiness is not None:
        options["readiness"] = readiness
    runtime = SocketBrokerRuntime(
        label="test broker",
        thread_name="test-broker",
        open_listener=open_listener or default_open,
        handler=handler or (lambda connection: 0),
        deactivate=deactivate,
        close=close or default_close,
        error_type=RuntimeError_,
        backlog=4,
        listener_timeout=0.2,
        client_timeout=30,
        **options,
    )
    return runtime, calls
```

import に `from agent_container.broker.runtime import open_connection` を追加する。

- [ ] **Step 2: 失敗する test を書く**

`tests/container/test_broker_runtime.py` の末尾（`if __name__ == "__main__":` の前）に追加:

```python
class OpenConnectionTest(unittest.TestCase):
    def test_sets_timeout_reads_peer_uid_and_opens_an_unbuffered_stream(self) -> None:
        client = FakeClient(4040)
        connection = open_connection(client, timeout=7)
        self.assertEqual(client.timeout, 7)
        self.assertEqual(
            client.credential_calls, [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]
        )
        self.assertEqual(connection, Connection(client, client.stream, 4040))


class ThreadedSocketBrokerRuntimeTest(unittest.TestCase):
    def test_rejects_unknown_concurrency_mode(self) -> None:
        with self.assertRaises(ValueError) as raised:
            make_runtime(FakeListener(), concurrency="process")
        self.assertEqual(str(raised.exception), "test broker concurrency mode is invalid")

    def test_thread_mode_serves_connections_concurrently_on_named_workers(self) -> None:
        clients = (FakeClient(1010), FakeClient(2020))
        listener = FakeListener(clients)
        both_entered = threading.Event()
        release = threading.Event()
        seen: list[tuple[int, str]] = []

        def blocking_handler(connection: Connection) -> None:
            seen.append((connection.peer_uid, threading.current_thread().name))
            if len(seen) == 2:
                both_entered.set()
            release.wait(2)

        runtime, calls = make_runtime(
            listener,
            blocking_handler,
            concurrency="thread",
            worker_thread_name="test-worker",
        )
        runtime.start()
        try:
            self.assertTrue(both_entered.wait(1))
            with runtime.worker_lock:
                self.assertEqual(len(runtime.workers), 2)
        finally:
            release.set()
            runtime.stop(join_timeout=2)

        self.assertEqual(sorted(seen), [(1010, "test-worker"), (2020, "test-worker")])
        self.assertTrue(all(client.closed for client in clients))
        self.assertTrue(all(client.stream.closed for client in clients))
        self.assertEqual(runtime.workers, set())
        self.assertFalse(runtime.wait_failed(0))
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)

    def test_thread_mode_default_worker_name_derives_from_thread_name(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        names: list[str] = []
        handled = threading.Event()

        def handler(connection: Connection) -> None:
            names.append(threading.current_thread().name)
            handled.set()

        runtime, _ = make_runtime(listener, handler, concurrency="thread")
        runtime.start()
        try:
            self.assertTrue(handled.wait(1))
        finally:
            runtime.stop(join_timeout=2)
        self.assertEqual(names, ["test-broker-worker"])

    def test_raw_client_hands_the_socket_without_opening_a_connection(self) -> None:
        for concurrency in ("inline", "thread"):
            with self.subTest(concurrency=concurrency):
                client = FakeClient(1010)
                listener = FakeListener((client,))
                received: list[object] = []
                handled = threading.Event()

                def handler(raw: object) -> None:
                    received.append(raw)
                    handled.set()

                runtime, _ = make_runtime(
                    listener, handler, concurrency=concurrency, raw_client=True
                )
                runtime.start()
                try:
                    self.assertTrue(handled.wait(1))
                finally:
                    runtime.stop(join_timeout=2)

                self.assertEqual(received, [client])
                self.assertIsNone(client.timeout)
                self.assertEqual(client.credential_calls, [])
                self.assertFalse(client.stream.closed)
                self.assertTrue(client.closed)

    def test_thread_mode_swallows_os_errors_but_reports_other_worker_failures(self) -> None:
        for error, fatal in (
            (ConnectionResetError("private-client-marker"), False),
            (RuntimeError("private-worker-marker"), True),
        ):
            with self.subTest(fatal=fatal):
                client = FakeClient(1010)
                listener = FakeListener((client,))
                handled = threading.Event()

                def fail(connection: Connection) -> None:
                    handled.set()
                    raise error

                runtime, calls = make_runtime(listener, fail, concurrency="thread")
                runtime.start()
                self.assertTrue(handled.wait(1))
                self.assertEqual(runtime.wait_failed(0.2), fatal)
                self.assertEqual(runtime.stop_event.is_set(), fatal)
                if fatal:
                    with self.assertRaises(RuntimeError_) as raised:
                        runtime.stop(join_timeout=2)
                    self.assertEqual(str(raised.exception), "test broker failed")
                    self.assertNotIn("private-worker-marker", str(raised.exception))
                    self.assertIs(runtime.error, error)
                else:
                    runtime.stop(join_timeout=2)
                    self.assertIsNone(runtime.error)

                self.assertTrue(client.closed)
                self.assertTrue(runtime.exited)
                self.assertEqual(calls["close"], 1)

    def test_accept_loop_failure_sets_failed_in_inline_mode_too(self) -> None:
        class BrokenListener(FakeListener):
            def accept(self):
                raise RuntimeError("private-accept-marker")

        listener = BrokenListener()
        runtime, _ = make_runtime(listener)
        runtime.start()
        self.assertTrue(runtime.wait_failed(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")

    def test_deactivate_after_join_runs_deactivate_once_workers_have_finished(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        worker_entered = threading.Event()
        deactivations_seen: list[int] = []

        runtime, calls = make_runtime(
            listener, None, concurrency="thread", deactivate_after_join=True
        )

        def finish_after_listener_close(connection: Connection) -> None:
            worker_entered.set()
            listener._wake.wait(1)
            deactivations_seen.append(calls["deactivate"])

        runtime.handler = finish_after_listener_close
        runtime.start()
        self.assertTrue(worker_entered.wait(1))
        runtime.stop(join_timeout=2)

        self.assertEqual(deactivations_seen, [0])
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)

    def test_deactivate_after_join_still_deactivates_when_a_worker_does_not_stop(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        release = threading.Event()
        entered = threading.Event()

        def stuck(connection: Connection) -> None:
            entered.set()
            release.wait(5)

        runtime, calls = make_runtime(
            listener, stuck, concurrency="thread", deactivate_after_join=True
        )
        runtime.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=0.05)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 0)
        self.assertFalse(runtime.exited)

        release.set()
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
        self.assertTrue(client.closed)

    def test_deactivate_before_join_is_still_the_default(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        worker_entered = threading.Event()
        deactivations_seen: list[int] = []

        runtime, calls = make_runtime(listener, None, concurrency="thread")

        def observe(connection: Connection) -> None:
            worker_entered.set()
            listener._wake.wait(1)
            deactivations_seen.append(calls["deactivate"])

        runtime.handler = observe
        runtime.start()
        self.assertTrue(worker_entered.wait(1))
        runtime.stop(join_timeout=2)
        self.assertEqual(deactivations_seen, [1])
```

- [ ] **Step 3: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime.OpenConnectionTest tests.container.test_broker_runtime.ThreadedSocketBrokerRuntimeTest -v 2>&1 | tail -5`
Expected: import で `open_connection` が無く ImportError（module 全体が読めない）。既存 test を壊さないよう Step 4 を直ちに行う。

- [ ] **Step 4: 実装**

`src/agent_container/broker/runtime.py` の `import` に `import time` を追加し、`_PEER_CREDENTIAL_BYTES = 12` 以降（`Connection` と `SocketBrokerRuntime`）を次に置き換える:

```python
_PEER_CREDENTIAL_BYTES = 12
_CONCURRENCY_MODES = frozenset({"inline", "thread"})


@dataclass(frozen=True)
class Connection:
    client: Any
    stream: Any
    peer_uid: int


def open_connection(client: Any, *, timeout: float) -> Connection:
    client.settimeout(timeout)
    credentials = client.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        _PEER_CREDENTIAL_BYTES,
    )
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    return Connection(client, client.makefile("rwb", buffering=0), peer_uid)


@dataclass
class SocketBrokerRuntime:
    label: str
    thread_name: str
    open_listener: Callable[[int], Any]
    # raw_client=False: handler(Connection). raw_client=True: handler(client socket).
    handler: Callable[[Any], object]
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = field(default_factory=AlwaysReady)
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    concurrency: str = "inline"
    worker_thread_name: str = ""
    raw_client: bool = False
    deactivate_after_join: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    failed: threading.Event = field(default_factory=threading.Event, init=False)
    thread: Any | None = field(default=None, init=False)
    listener: Any | None = field(default=None, init=False, repr=False)
    error: BaseException | None = field(default=None, init=False, repr=False)
    exited: bool = field(default=False, init=False, repr=False)
    workers: set[Any] = field(default_factory=set, init=False, repr=False)
    worker_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.concurrency not in _CONCURRENCY_MODES:
            raise ValueError(f"{self.label} concurrency mode is invalid")

    def start(self) -> None:
        if self.thread is not None or self.exited:
            raise self.error_type(f"{self.label} failed to start")
        listener: Any | None = None
        try:
            listener = self.open_listener(self.backlog)
            listener.settimeout(self.listener_timeout)
            self.listener = listener
            # Keep this as attribute access on the threading module: the
            # handover runtime tests patch threading.Thread globally and rely
            # on the kernel picking the patched class up.
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name=self.thread_name,
                daemon=True,
            )
            thread.start()
            self.thread = thread
        except BaseException:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            cleanup_complete = False
            try:
                self.close()
            except (OSError, ValueError):
                pass
            else:
                cleanup_complete = True
            self.exited = cleanup_complete
            raise self.error_type(f"{self.label} failed to start") from None

    def _serve(self, listener: Any) -> None:
        try:
            while not self.stop_event.is_set():
                if self.readiness.wait(self.listener_timeout):
                    break
            else:
                return
            while not self.stop_event.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                if self.concurrency == "thread":
                    self._start_worker(client)
                else:
                    with client:
                        self._handle_client(client)
        except BaseException as error:
            self.error = error
            self.failed.set()

    def _handle_client(self, client: Any) -> None:
        if self.raw_client:
            self.handler(client)
            return
        connection = open_connection(client, timeout=self.client_timeout)
        try:
            self.handler(connection)
        finally:
            connection.stream.close()

    def _start_worker(self, client: Any) -> None:
        thread = threading.Thread(
            target=self._run_worker,
            args=(client,),
            name=self.worker_thread_name or f"{self.thread_name}-worker",
            daemon=True,
        )
        with self.worker_lock:
            self.workers.add(thread)
        try:
            thread.start()
        except BaseException:
            with self.worker_lock:
                self.workers.discard(thread)
            client.close()
            raise

    def _run_worker(self, client: Any) -> None:
        try:
            self._handle_client(client)
        except OSError:
            pass
        except BaseException as error:
            self.error = error
            self.stop_event.set()
            self.failed.set()
        finally:
            client.close()
            with self.worker_lock:
                self.workers.discard(threading.current_thread())

    def wait_failed(self, timeout: float) -> bool:
        return self.failed.wait(timeout)

    def _join_workers(self, join_timeout: float) -> bool:
        deadline = time.monotonic() + join_timeout
        with self.worker_lock:
            workers = tuple(self.workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        with self.worker_lock:
            return bool(self.workers)

    def stop(self, *, join_timeout: float) -> None:
        if self.exited:
            return
        self.stop_event.set()
        if not self.deactivate_after_join:
            self.deactivate()
        cleanup_failed = False
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self.listener = None

        did_not_stop = False
        if self.thread is not None:
            self.thread.join(timeout=join_timeout)
            did_not_stop = self.thread.is_alive()
        if self._join_workers(join_timeout):
            did_not_stop = True
        if self.deactivate_after_join:
            self.deactivate()

        if did_not_stop:
            raise self.error_type(f"{self.label} did not stop") from None

        try:
            self.close()
        except (OSError, ValueError):
            cleanup_failed = True
        else:
            self.exited = True

        if cleanup_failed:
            raise self.error_type(f"{self.label} cleanup failed") from None
        if self.error is not None:
            raise self.error_type(f"{self.label} failed") from None
```

inline path の呼び出し順は 6-2 と同一（`with client:` → `settimeout` → `SO_PEERCRED` → `makefile` → handler → `stream.close()`）で、`_handle_client`／`open_connection` への切り出しは関数境界だけが変わる。`stop` は `deactivate_after_join=False`、worker 無しのとき 6-2 と同じ順序（`_join_workers` は空集合で即 `False`）。

- [ ] **Step 5: PASS と回帰を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime tests.container.test_handover_broker_runtime tests.container.test_broker_readiness -v 2>&1 | tail -3`
Expected: OK（`test_broker_runtime` は `0ca61c2` の 24 + Task 4 の 1 + 本 task の 10 = 35）。

- [ ] **Step 6: lint**

Run: `bin/lint`
Expected: pass。

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/broker/runtime.py tests/container/test_broker_runtime.py
git commit -m "feat: add threaded workers, raw client hand-off and late deactivate to the broker kernel runtime

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `EgressBrokerRuntime` を `SocketBrokerRuntime` に乗せ替える

**Files:**
- Modify: `src/agent_container/egress_broker_runtime.py`
- Test: `tests/container/test_egress_broker_runtime.py`（不変）、`tests/container/test_podman.py`（不変）

**Interfaces:**
- Consumes: `SocketBrokerRuntime(..., concurrency="thread", worker_thread_name="egress-tunnel", raw_client=True, deactivate_after_join=True)`、`start()`、`stop(join_timeout=...)`、`wait_failed(timeout)`、`thread`、`open_connection(client, *, timeout)`
- Produces: `EgressBrokerRuntime` の公開 API と test が触る private 不変

- [ ] **Step 1: baseline**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker_runtime -v 2>&1 | tail -3`
Expected: 12 tests OK。

- [ ] **Step 2: module を書き換える**

`src/agent_container/egress_broker_runtime.py` 全体を次に置き換える（`EgressRuntimeMount`、tunnel 予約 3 method、`_write_response`、`_handle_client` の handshake／relay／audit 本体は旧実装のまま）:

```python
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import socket
import threading
from typing import Any
from typing import BinaryIO

from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.broker.runtime import open_connection
from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import encode_response_frame
from agent_container.egress_broker_protocol import read_request_frame
from agent_container.egress_gateway import connect_target
from agent_container.egress_gateway import RelayLimits
from agent_container.egress_gateway import relay_tunnel
from agent_container.egress_gateway import resolve_target
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


_LISTENER_TIMEOUT_SECONDS = 0.2
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 32
_MAX_ACTIVE_TUNNELS = 32
_MAX_CREATED_TUNNELS = 128
_CLIENT_TIMEOUT_SECONDS = 30


class EgressBrokerRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class EgressRuntimeMount:
    run_dir: Path
    project_id: str
    agent: str

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "broker.sock"

    @property
    def capability_path(self) -> Path:
        return self.run_dir / "capability"

    @property
    def container_name(self) -> str:
        label = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:16]
        return f"agent-egress-{label}"


@dataclass
class EgressBrokerRuntime(AbstractContextManager[EgressRuntimeMount]):
    session: EgressBrokerSession
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)
    _limit_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _next_reservation: int = field(default=1, init=False, repr=False)
    _active_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_tunnels: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="egress broker",
            thread_name="egress-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            # Looked up per call so tests can patch _handle_client on the instance.
            handler=lambda client: self._handle_client(client),
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=EgressBrokerRuntimeError,
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
            concurrency="thread",
            worker_thread_name="egress-tunnel",
            raw_client=True,
            deactivate_after_join=True,
        )

    @classmethod
    def create(
        cls, layout: StateLayout, agent: str, policy: EgressPolicy
    ) -> "EgressBrokerRuntime":
        return cls(EgressBrokerSession.create(layout, agent, policy))

    def _reserve_tunnel(self) -> int:
        with self._limit_lock:
            pending_creations = len(
                self._active_reservations - self._created_reservations
            )
            if (
                len(self._active_reservations) >= _MAX_ACTIVE_TUNNELS
                or self._created_tunnels + pending_creations
                >= _MAX_CREATED_TUNNELS
            ):
                raise ValueError("egress tunnel limit reached")
            reservation = self._next_reservation
            self._next_reservation += 1
            self._active_reservations.add(reservation)
            return reservation

    def _mark_tunnel_created(self, reservation: int) -> None:
        with self._limit_lock:
            if (
                reservation not in self._active_reservations
                or reservation in self._created_reservations
                or self._created_tunnels >= _MAX_CREATED_TUNNELS
            ):
                raise ValueError("egress tunnel reservation is invalid")
            self._created_reservations.add(reservation)
            self._created_tunnels += 1

    def _release_tunnel(self, reservation: int) -> None:
        with self._limit_lock:
            if reservation not in self._active_reservations:
                raise ValueError("egress tunnel reservation is invalid")
            self._active_reservations.remove(reservation)
            self._created_reservations.discard(reservation)

    @property
    def _thread(self) -> Any | None:
        return self._runtime.thread

    def __enter__(self) -> EgressRuntimeMount:
        self._runtime.start()
        return EgressRuntimeMount(
            self.session.run_dir, self.session.project_id, self.session.agent
        )

    def wait_failed(self, timeout: float) -> bool:
        return self._runtime.wait_failed(timeout)

    def _write_response(
        self, stream: BinaryIO, status: str, code: str
    ) -> None:
        stream.write(encode_response_frame(EgressResponse(1, status, code)))
        stream.flush()

    def _handle_client(self, client: socket.socket) -> None:
        connection = open_connection(client, timeout=_CLIENT_TIMEOUT_SECONDS)
        stream: BinaryIO = connection.stream
        peer_uid = connection.peer_uid
        reservation: int | None = None
        upstream: socket.socket | None = None
        try:
            try:
                request = read_request_frame(stream)
                domain = self.session.authorize(request, peer_uid)
            except (OSError, ValueError) as error:
                stage = (
                    "policy"
                    if getattr(error, "stage", None) == "policy"
                    else "authentication"
                )
                self._write_response(stream, "denied", stage)
                self.session.audit("denied", stage=stage)
                return
            try:
                reservation = self._reserve_tunnel()
            except ValueError:
                self._write_response(stream, "denied", "limit")
                self.session.audit("denied", stage="limit")
                return
            try:
                targets = resolve_target(domain)
            except ValueError:
                self._write_response(stream, "denied", "resolve")
                self.session.audit("denied", stage="resolve")
                return
            try:
                upstream = connect_target(targets[0])
            except ValueError:
                self._write_response(stream, "error", "connect")
                self.session.audit("error", stage="connect")
                return
            self._mark_tunnel_created(reservation)
            self._write_response(stream, "ok", "connect")
            try:
                counts = relay_tunnel(client, upstream, RelayLimits())
            except ValueError:
                self.session.audit("error", stage="relay")
                return
            self.session.audit(
                "ok",
                bytes_from_client=counts.from_client,
                bytes_from_upstream=counts.from_upstream,
            )
        finally:
            if upstream is not None:
                upstream.close()
            if reservation is not None:
                self._release_tunnel(reservation)
            stream.close()

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
```

削除されるもの: `import struct`、`import time`、`_PEER_CREDENTIAL_BYTES`、`_stop`／`_thread`（field）／`_listener`／`_error`／`_failed`／`_exited`／`_worker_lock`／`_workers` の field、`_serve`、`_start_worker`、`_run_worker`。`_thread` は property として残る（`test_context_starts_listener_and_cleans_exact_session` が `runtime._thread.is_alive()` を見る）。

既存 test との対応: (a) `test_context_starts_listener_and_cleans_exact_session` は backlog `32`、listener timeout `0.2`、`_thread`、停止時に `deactivate_calls == 1`／`close_calls == 1`。(b) `test_start_failure_cleans_and_raises_fixed_error` は kernel `start` の失敗 path で `session.close()` が 1 回。(c) `_handle_client(client)` を直接呼ぶ 4 test は `open_connection` が `settimeout`／`getsockopt`（`credential_calls` 1 件）／`makefile` を行う。(d) `test_listener_dispatches_connections_concurrently` は `mock.patch.object(runtime, "_handle_client", ...)` で差し替えた関数を lambda が呼び出し時に解決し、raw client（`FakeClient`）をそのまま渡す。worker の `finally: client.close()` で `client.closed`。(e) `test_client_disconnect_is_graceful_but_worker_bug_is_fatal` は `OSError` 握り潰し／`RuntimeError` で `wait_failed` True と `egress broker failed`。(f) `test_shutdown_allows_inflight_worker_to_finish_final_audit` は `deactivate_after_join=True` で worker の最終 audit の後に `deactivate`。

- [ ] **Step 3: 既存 test が不変のまま PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker_runtime tests.container.test_podman tests.container.test_agentctl -v 2>&1 | tail -3`
Expected: OK。`git status --short tests/` に egress の既存 test が現れない。

- [ ] **Step 4: 実 socket integration**

Run: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_egress_broker_socket -v 2>&1 | tail -3`
Expected: 2 tests OK（`openssl` が必要。無ければ skip ではなく失敗するので、その場合は理由を報告して CI の Podman integration に委ねる）。

- [ ] **Step 5: lint**

Run: `bin/lint`
Expected: pass。

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/egress_broker_runtime.py
git commit -m "refactor: back the egress runtime with the broker kernel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: docs と全体検証

**Files:**
- Modify: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: spec の `broker/frame.py` 行を実装に合わせる**

「構成」節の表で、`| \`broker/frame.py\` |` の行の 責務 cell 内、`` `FrameSchema(label, stream_label, fields, max_bytes, json)`を受け取り `` を `` `FrameSchema(label, stream_label, fields, max_bytes, json, frame_label=None)`を受け取り `` に、`` `label`はerror messageの接頭辞、`` を `` `label`はschema／encode error messageの接頭辞、`frame_label`はframing／JSON error messageの接頭辞（`None`なら`label`。egressのように両者の接頭辞が違うbrokerのために6-3で追加）、`` に置き換える。

- [ ] **Step 2: spec の `broker/runtime.py` 行を実装に合わせる**

同表の `| \`broker/runtime.py\` |` の行の 責務 cell で、`` `create_private_file(path, body, *, label)`（`O_CREAT|O_EXCL|O_NOFOLLOW`、mode `0600`、ascii、fsync） `` を `` `create_private_file(path, body, *, label, mode=0o600)`（`O_CREAT|O_EXCL|O_NOFOLLOW`、`mode`で`open`と`fchmod`、ascii、fsync。egressのcapabilityは`0400`） `` に置き換える。

`` `SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, readiness=AlwaysReady(), backlog, listener_timeout, client_timeout)` `` を `` `SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, readiness=AlwaysReady(), backlog, listener_timeout, client_timeout, concurrency="inline", worker_thread_name="", raw_client=False, deactivate_after_join=False)` `` に置き換える。

`` 接続毎に`settimeout`、`SO_PEERCRED`でpeer uidを取り、`handler(Connection(client, stream, peer_uid))`を呼ぶ（`Connection`はfrozen dataclass。egressのようにsocket本体を要するbrokerも同じhandlerで受ける）。 `` を `` 接続毎に公開helper `open_connection(client, *, timeout)`で`settimeout`、`SO_PEERCRED`でpeer uidを取り、`handler(Connection(client, stream, peer_uid))`を呼ぶ（`Connection`はfrozen dataclass）。`raw_client=True`ではConnectionを開かず`handler(client)`にsocket本体を渡し、brokerが自分で`open_connection`を呼ぶ（egress）。 `` に置き換える。

`` `stop(join_timeout=...)`は`stop_event.set → deactivate → listener close → join → close`の順で `` を `` `stop(join_timeout=...)`は`stop_event.set → deactivate → listener close → accept thread join → worker join → close`の順（`deactivate_after_join=True`ならdeactivateをworker joinの後、`did not stop`の判定前に呼ぶ）で `` に置き換える。

末尾の `` `concurrency="thread"`（egress）は6-3で追加する。 `` を次に置き換える:

```
`concurrency="thread"`（6-3で追加）は接続毎にdaemon worker thread（`worker_thread_name`、既定は`{thread_name}-worker`）を起こし、workerの`OSError`は接続単位の失敗として握り潰し、それ以外の例外は`error`／`failed`を立てて`stop_event`をsetする。`wait_failed(timeout)`は`failed` eventを待つ（accept loopの失敗でもsetされる）。inline方式のclientは`with client:`、thread方式のclientは`finally: client.close()`で閉じる（それぞれhandover／egressの乗せ替え前の閉じ方）。
```

- [ ] **Step 3: spec の `broker/capability.py` 行に egress adapter の据え置きを追記する**

同表の `| \`broker/capability.py\` |` の行の 責務 cell 末尾、`` 6-4で乗せ替える際に、この厳格化を受け入れるかparameter化するかを明示的に決める。 `` の直後に追加:

```
egressの`egress_adapter._read_capability`も同種で、mode `0400`／`0444`を受理し、owner・size検査と`O_NONBLOCK`を持たず、既存testは`0600`を拒否することを固定している。`open_gateway_tunnel`はtimeoutを設定しないため`connect_unix`にも乗らない。6-3ではadapterを据え置き、厳格化の受け入れかparameter化かは6-4でgithubと一緒に決める。
```

- [ ] **Step 4: spec の設計入力段落に 6-3 の決定を追記する**

「構成」節の、`6-2では\`handover_broker_transport.py\`を据え置いた。` で始まる段落の直後に、次の段落を追加する:

```
6-3ではegressを乗せ替えた。既存test `tests/container/test_egress_broker_runtime.py`は`EgressBrokerRuntime._handle_client(client)`をserve pathの単位として固定し（差し替えた関数がclientを引数に呼ばれること、直接呼ぶと`SO_PEERCRED`が取られること）、egressのfake clientはcontext managerでないため、kernelが先に`Connection`を開く設計ではpeer credentialとstreamが二重になる。そこで「socket本体を要するbrokerも同じhandlerで受ける」という本文書の当初案から外れ、`raw_client=True`をkernelのseamとして追加した。kernelはacceptとworker管理と`client.close()`だけを担い、egressは公開helper `open_connection`で`Connection`を開く。egressのcapability fileはmode `0400`のまま（`create_private_file(mode=...)`）、response frameにはkernelの`MAX_METADATA_BYTES`上限が付くが固定語彙のため到達不能、adapterは前段落のとおり据え置き。golden（`tests/container/test_broker_egress_golden.py`）は`0ca61c2`のencoder／writerで生成した。
```

- [ ] **Step 5: spec の PR 表 6-3 行を実装に合わせる**

「乗せ替えの順序とPR分割」の表で、`| 6-3 |` の行の「kernelに入る部品」cell を `` `FrameSchema.frame_label`、`create_private_file(mode)`、`open_connection`、runtimeの`concurrency="thread"`・worker回収・`wait_failed`・`raw_client`・`deactivate_after_join` `` に置き換える。

- [ ] **Step 6: CHANGELOG**

`## [Unreleased]` → `### Added` の末尾（6-2 の行の直後）に追加:

```markdown
- Phase 6 stage 1の3番目の乗せ替えとして、egress brokerのprotocol、session、runtimeを共通broker kernelの上に移しました。kernelには`FrameSchema.frame_label`（framing／JSON errorの接頭辞をschema errorと分ける）、`create_private_file`の`mode`、`open_connection`、`SocketBrokerRuntime`の`concurrency="thread"`（接続毎のworker threadと回収）・`wait_failed`・`raw_client`・`deactivate_after_join`を既定値を変えずに追加しました。wire byte、audit行、error message、停止順序、既存のegress testは変更していません。kernel化前のencoderとwriterで生成したgolden fixtureを`tests/container/test_broker_egress_golden.py`に固定しました。container側の`egress_adapter.py`は6-4でgithubと一緒に扱うため据え置きです。
```

- [ ] **Step 7: 全体検証**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | grep -E "^Ran|^OK|FAILED"
PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | grep -E "^Ran|^OK|FAILED"
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_egress_broker_socket tests.integration.test_handover_broker_socket 2>&1 | grep -E "^Ran|^OK|FAILED"
bin/lint
git diff --stat main -- tests/
git diff --stat main -- src/
```
Expected: container 1061 + 18 = 1079 OK（`0ca61c2` の loader 件数 1061 に、egress golden 5、frame_label 2、mode 1、open_connection 1、threaded runtime 9 を加える。subTest は件数に入らない）、codex 44 OK、socket integration OK、lint pass。`tests/` の差分は `test_broker_egress_golden.py`（新規）、`test_broker_frame.py`、`test_broker_runtime.py` の 3 file のみ。`src/` の差分は `broker/frame.py`、`broker/runtime.py`、`egress_broker_protocol.py`、`egress_broker.py`、`egress_broker_runtime.py` の 5 file のみ。egress の既存 test 7 file、`test_podman.py`、handover の全 file、`egress_adapter.py`、`podman.py`、`agentctl.py`、`state.py` は `main` と byte 一致（`git diff main -- <path>` が空）。

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-09-04-broker-kernel-design.md CHANGELOG.md
git commit -m "docs: record the broker kernel 6-3 landing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

push と PR は controller が最終 review の後に行う。PR 本文には `git diff --stat main -- tests/` の出力（既存 test 不変の証拠）と kernel unit test の追加数、spec からの逸脱（`raw_client`）と adapter 据え置きを書く。

---

## Verification（end-to-end）

1. golden: `test_broker_egress_golden.py` が kernel 化の前後で同じ frame byte と audit byte を要求する（Task 1 で現行 code に対して PASS、Task 3／5 以降も PASS）。`test_broker_frame_golden.py`／`test_broker_audit_golden.py`（handover）も引き続き PASS。
2. 既存 test 不変: `git diff --stat main -- tests/` に egress の既存 7 file、`test_podman.py`、handover の既存 file が現れない。
3. 実 socket: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1` で `tests/integration/test_egress_broker_socket.py` が adapter → kernel runtime（thread 方式）→ session → relay → audit を往復する。
4. image contract: `tests/container/test_image.py` が `egress_adapter`／`egress_runtime` の import probe で kernel subpackage を含めて通る（6-1 の copy 修正済み）。
5. CI: Unit tests と Podman integration（`tests/integration/test_egress_podman.py`）。
6. 実 host smoke（`docs/egress-domain-allowlist-smoke-test.md` の無変更再実行）は 6-6 でまとめて行う。

## Self-review 記録

- Spec coverage（6-3 の範囲）: 設計入力 (1) frame 用 label → Task 2、egress protocol 乗せ替え → Task 3、capability mode → Task 4、session（audit／資源 helper）→ Task 5、`concurrency="thread"`・worker 回収・`wait_failed`・deactivate 順序 → Task 6、runtime 乗せ替え → Task 7、golden（wire byte と audit 行の同一性）→ Task 1、spec／CHANGELOG 更新と spec からの逸脱の記録 → Task 8。adapter 据え置きは Context で決定し Task 8 で spec に記録。
- 型の一貫性: `FrameSchema(..., frame_label=)`／`frame_prefix`（Task 2）を Task 3 で同名で使う。`create_private_file(path, body, *, label, mode)`（Task 4）を Task 5 で同名同引数で使う。`open_connection(client, *, timeout) -> Connection`、`SocketBrokerRuntime(..., concurrency, worker_thread_name, raw_client, deactivate_after_join)`、`wait_failed(timeout)`、属性 `thread`／`failed`／`workers`／`worker_lock`（Task 6）を Task 7 で同名で使う。`Connection.client`／`.stream`／`.peer_uid` は 6-2 のまま。
- Placeholder: なし。golden byte は実測値を転記済み。
- 既知の注意: Task 6 の test は実 thread を使う。`FakeListener.accept` は 10 ms の wait で `TimeoutError` を返すので stop は速く収束する。`test_deactivate_after_join_still_deactivates_when_a_worker_does_not_stop` は `join_timeout=0.05` で `did not stop` を作り、`release` 後に 2 回目の `stop` で回収する（2 回目でも `deactivate` が再度呼ばれるのは egress の旧 `__exit__` と同じ）。
- 6-2 plan との差: 6-2 は `Claude-Session` trailer を commit に付けたが、本 session の web session id は不明なので付けない。
- 最終 whole-branch review で判明: Task 7 の「削除されるもの」に `_listener` field を含めたが、`tests/integration/test_egress_podman.py:206-207` が `runtime._listener` を使う。private surface の列挙が unit test file だけを見ていたため。fix wave で `_listener` を property として復元し、`tests/container/test_egress_broker_runtime_surface.py` で lifecycle を固定した。6-4 の plan では `tests/` 全体（integration を含む）を `runtime\._`／`session\._` で grep して preserved private surface を列挙する。
