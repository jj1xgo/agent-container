# Phase 6-2: broker kernel `runtime` / `audit` / `readiness` + handover runtime 乗せ替え Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `src/agent_container/broker/` に `audit.py`（private な append-only audit log）、`readiness.py`（`ReadinessGate` protocol と `AlwaysReady`）、`runtime.py`（run directory／capability file／private socket の生成と回収、および accept loop・thread・stop・error capture の lifecycle `SocketBrokerRuntime`）を抽出し、handover broker の `HandoverBrokerSession`（`handover_broker.py`）と `HandoverBrokerRuntime`（`handover_broker_runtime.py`）をその上に乗せ替える。既存 handover test、audit 行、error message、呼び出し順序は一切変えない。

**Architecture:** kernel は broker 固有 module を import しない（`broker/runtime.py` は同じ kernel の `broker/capability.py` から `CAPABILITY_PATTERN` を、`broker/readiness.py` から `ReadinessGate`／`AlwaysReady` を import する）。`HandoverBrokerSession` は authorize／audit record 構築／publication guard／lifecycle lock を保持し、file・socket の生成回収と audit の open／append を kernel に委ねる。`HandoverBrokerRuntime` は `SocketBrokerRuntime` を合成し、既存 test が触る `_thread`／`_stop` を property で委譲する。`HandoverRuntimeMount` と `podman.py` は変えない。振る舞い保存は (a) 既存 test 不変、(b) kernel 化前の writer（commit `4c555fd`）で生成した audit 行 golden、で証明する。

**Tech Stack:** Python 3.11、標準 library のみ（`os`, `socket`, `threading`, `secrets`, `json`, `stat`, `dataclasses`, `typing.Protocol`）、`unittest`、ruff 0.16.4（`bin/lint`）。

**Spec:** `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`（Phase 6 / stage 1、「乗せ替えの順序とPR分割」の **6-2** 行）。6-3 以降は本 PR の merge 後に別 plan を書く。

## Context

6-1（PR #89）で `broker/frame.py` と `broker/capability.py` を抽出し、handover の protocol と container 側 client を乗せ替えた。6-2 は host 側の残り、すなわち runtime lifecycle と audit を kernel に移す。handover を先にする理由は 6-1 と同じ（operation が 1 つ、追加保証なし、audit が最単純）。

**このplanで決めたこと（spec が 6-2 に委ねていた点）:**
- `handover_broker_transport.py` は **6-2 では触らない**。transport の `_read_exact` は `_RequestFailure("schema")`、長さ検査は `_RequestFailure("size")` を出し、その区別が audit の `stage` に入る（spec「構成」節の設計入力 (4)）。kernel `read_frame` へ単純に置き換えると `size` が `schema` に畳まれて audit 行が変わるため、stage 1 の原則に反する。transport の kernel 化は、例外種別の口を kernel に設けるかを含めて stage 2 で扱う。
- kernel `runtime.py` は 2 層に分ける。**資源 helper**（`create_private_file`、`allocate_run_dir`、`generate_capability`、`bind_private_listener`、`remove_runtime_artifacts`）は `HandoverBrokerSession` が使い、**lifecycle**（`SocketBrokerRuntime`）は `HandoverBrokerRuntime` が使う。既存 test が session fake（`open_listener`／`deactivate`／`close`）と runtime を別々に固定しているため、この境界を保つのが振る舞い保存の最短経路である。
- egress の `_open_audit_file`／`_write_audit_record`（`egress_broker.py:64-115`）は handover と **label 以外同一** であることを確認した。kernel `AuditLog(path, label=...)` は両方をそのまま賄える（egress の乗せ替えは 6-3）。
- 既存 test の patch 経路: `tests/container/test_handover_broker.py` は `agent_container.handover_broker.os.chmod`／`.os.open`／`.os.fstat`／`.socket.socket` を patch する。これらは module 属性（= `os`／`socket` module そのもの）への patch なので、kernel が `os.chmod(...)`／`socket.socket(...)` を **module 属性参照で** 呼べば kernel 内にも届く。`tests/container/test_handover_broker_runtime.py` は `agent_container.handover_broker_runtime.threading.Thread` を patch し（同様に global）、`agent_container.handover_broker_runtime._STOP_TIMEOUT_SECONDS` を **`__exit__` の直前に** patch する。したがって kernel は `threading.Thread(...)` を属性参照で作り、handover runtime は `_STOP_TIMEOUT_SECONDS` を `__exit__` 時に読んで kernel の `stop(join_timeout=...)` に渡す。

## Global Constraints

- kernel（`src/agent_container/broker/`）は `agent_container.handover_*` / `egress_*` / `github_*` / `family_*` / `state` を import しない。
- 次の既存 test file は **1 byte も変更しない**: `tests/container/test_handover_broker.py`、`test_handover_broker_runtime.py`、`test_handover_broker_transport.py`、`test_handover_broker_protocol.py`、`test_handover_broker_client.py`、`test_broker_frame_golden.py`、`tests/integration/test_handover_broker_socket.py`。変更が必要になったら止めて報告する。
- `src/agent_container/handover_broker_transport.py`、`handover_broker_protocol.py`、`handover_broker_client.py`、`podman.py`、`state.py` は変更しない。
- handover の例外型と message を保存する。session: `ValueError("handover broker cleanup failed")`、`ValueError("handover broker listener state is invalid")`、`ValueError("handover broker socket path is too long")`、`FileExistsError("handover broker socket path already exists")`、`FileExistsError("could not allocate handover broker runtime")`、`RuntimeError("generated handover broker capability has invalid format")`、audit: `ValueError("handover broker audit file must be a regular non-symlink file")`、`PermissionError("handover broker audit file must have mode 0600")`、`PermissionError("handover broker audit file must be owned by the current user")`、`ValueError("handover broker audit file changed during validation")`、`OSError("handover broker audit write failed")`、`OSError("handover broker private file write failed")`。runtime: `HandoverBrokerRuntimeError("handover broker failed to start" / "handover broker did not stop" / "handover broker cleanup failed" / "handover broker failed")`、すべて `from None`。
- audit 行の serialization は `json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"` を ascii で書く。record の key と timestamp 形式は呼び出し側（session）が決める。
- `HandoverRuntimeMount(run_dir)` と `podman.py` の argv は変えない。
- 作業は `.worktrees/feat-broker-kernel-runtime`、branch `feat/broker-kernel-runtime`。worktree 作成後に `chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json`。
- test: `cd <worktree> && PYTHONPATH=src python3 -m unittest <module> -v`。lint: `bin/lint`。
- commit 末尾: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` と `Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm`。
- 既存 broker の bug を見つけても直さない（Issue 化）。

---

## File Structure

| path | 役割 |
| --- | --- |
| `docs/superpowers/plans/2026-09-04-broker-kernel-6-2-runtime-audit.md` | **Create.** 本 plan の写し |
| `tests/container/test_broker_audit_golden.py` | **Create.** kernel 化前 writer（`4c555fd`）で生成した audit 行 byte を固定 |
| `src/agent_container/broker/audit.py` | **Create.** `AuditLog` |
| `src/agent_container/broker/readiness.py` | **Create.** `ReadinessGate`、`AlwaysReady` |
| `src/agent_container/broker/runtime.py` | **Create.** 資源 helper 5 関数と `SocketBrokerRuntime` |
| `src/agent_container/handover_broker.py` | **Modify.** `_create_private_file`／`_open_audit_file`／`_write_audit_record`／capability 生成／run dir 割り当て／`open_listener` 本体／`close` の artifact 回収を kernel へ委ねる。公開名・patch 対象の `import os`／`import socket` は維持 |
| `src/agent_container/handover_broker_runtime.py` | **Modify.** `SocketBrokerRuntime` を合成。`_thread`／`_stop` は property。定数と `HandoverBrokerRuntimeError`、`HandoverRuntimeMount`、`create` は維持 |
| `tests/container/test_broker_audit.py` | **Create.** kernel `audit` の unit test |
| `tests/container/test_broker_readiness.py` | **Create.** kernel `readiness` の unit test |
| `tests/container/test_broker_runtime.py` | **Create.** kernel `runtime` の unit test（資源 helper と lifecycle） |
| `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` | **Modify.** 6-2 の決定（transport 据え置き、runtime の 2 層）を記録 |
| `CHANGELOG.md` | **Modify.** Unreleased / Added に 1 行 |

## Interfaces（全 task 共通の contract）

`src/agent_container/broker/audit.py`:

```python
class AuditLog:
    def __init__(self, path: Path, *, label: str) -> None   # label 例 "handover broker audit"
    path: Path
    label: str
    def open_descriptor(self) -> int
        # os.open(path, O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_NONBLOCK, 0o600)。失敗は
        # ValueError(f"{label} file must be a regular non-symlink file")。fstat が通常 file でなければ同 message、
        # mode != 0o600 は PermissionError(f"{label} file must have mode 0600")、uid 不一致は
        # PermissionError(f"{label} file must be owned by the current user")、os.stat(path, follow_symlinks=False) が
        # 失敗すれば ValueError(同 non-symlink message)、dev/ino 不一致は ValueError(f"{label} file changed during validation")。
        # 検証失敗時は descriptor を close してから raise。成功時は open のままの descriptor を返す。
    def validate(self) -> None                 # open_descriptor して close するだけ
    def append(self, record: Mapping[str, object]) -> None
        # body = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        # open_descriptor → os.write loop（written <= 0 なら OSError(f"{label} write failed")）→ os.fsync → close（finally）
```

`src/agent_container/broker/readiness.py`:

```python
class ReadinessGate(Protocol):
    def register(self, peer: int) -> None: ...
    def wait(self, timeout: float | None = None) -> bool: ...
    def is_ready(self) -> bool: ...

class AlwaysReady:
    def register(self, peer: int) -> None: ...   # 何もしない
    def wait(self, timeout: float | None = None) -> bool: ...  # 常に True
    def is_ready(self) -> bool: ...               # 常に True
```

`src/agent_container/broker/runtime.py`:

```python
MAX_UNIX_SOCKET_PATH_BYTES = 107

def create_private_file(path: Path, body: str, *, label: str) -> None
    # os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600) → fchmod 0o600 → ascii write loop
    # （written <= 0 なら OSError(f"{label} private file write failed")）→ fsync → close（finally）
def allocate_run_dir(project_root: Path, *, label: str, attempts: int = 8) -> tuple[str, Path]
    # 最大 attempts 回: run_id = secrets.token_hex(8); (project_root / run_id).mkdir(mode=0o700)。
    # FileExistsError なら次へ。尽きたら FileExistsError(f"could not allocate {label} runtime")
def generate_capability(*, label: str) -> str
    # secrets.token_urlsafe(32)。CAPABILITY_PATTERN に合わなければ RuntimeError(f"generated {label} capability has invalid format")
def bind_private_listener(socket_path: Path, *, backlog: int, label: str) -> socket.socket
    # len(os.fsencode(socket_path)) > 107 → ValueError(f"{label} socket path is too long")
    # exists() or is_symlink() → FileExistsError(f"{label} socket path already exists")
    # socket.socket(AF_UNIX, SOCK_STREAM) → bind(str(socket_path)) → os.chmod(socket_path, 0o600)（Path を渡す）→ listen(backlog)
    # 途中で Exception: listener.close()、lstat が S_ISSOCK なら unlink、再送出
def remove_runtime_artifacts(*, capability_path: Path, socket_path: Path, run_dir: Path) -> bool
    # (capability_path, S_ISREG), (socket_path, S_ISSOCK) の順に: lstat FileNotFoundError → continue、
    # OSError → failed、型不一致 → failed、unlink OSError → failed。最後に run_dir.rmdir()
    # （FileNotFoundError は無視、OSError → failed）。戻り値は failed（True = 回収失敗）

@dataclass
class SocketBrokerRuntime:
    label: str                                   # 例 "handover broker"（message 接頭辞）
    thread_name: str                             # 例 "handover-broker"
    open_listener: Callable[[int], Any]          # backlog → listener
    handler: Callable[[Any, int], object]        # (stream, peer_uid) → 任意
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = field(default_factory=AlwaysReady)
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    stop_event: threading.Event   (init=False)
    thread: Any | None            (init=False, None)
    listener: Any | None          (init=False, None)
    error: BaseException | None   (init=False, None)
    exited: bool                  (init=False, False)

    def start(self) -> None
        # thread が None でない、または exited → error_type(f"{label} failed to start")
        # try: listener = open_listener(backlog); listener.settimeout(listener_timeout); self.listener = listener;
        #      thread = threading.Thread(target=self._serve, args=(listener,), name=thread_name, daemon=True);
        #      thread.start(); self.thread = thread
        # except BaseException: listener があれば close（OSError は無視）; close() を試み OSError/ValueError は無視、
        #      成功したら exited = True; raise error_type(f"{label} failed to start") from None
    def _serve(self, listener) -> None
        # try: if not readiness.wait(): raise error_type(f"{label} readiness gate failed")
        #      while not stop_event.is_set(): accept（TimeoutError → continue、OSError → stop 済みなら break、でなければ raise）
        #        with client: client.settimeout(client_timeout); creds = client.getsockopt(SOL_SOCKET, SO_PEERCRED, 12);
        #        _pid, peer_uid, _gid = struct.unpack("3i", creds); stream = client.makefile("rwb", buffering=0);
        #        try: handler(stream, peer_uid) finally: stream.close()
        # except BaseException as error: self.error = error
    def stop(self, *, join_timeout: float) -> None
        # exited なら return。stop_event.set(); deactivate(); listener があれば close（OSError → cleanup_failed、成功で None）;
        # thread があれば join(timeout=join_timeout) して is_alive() → error_type(f"{label} did not stop") from None（close は呼ばない）;
        # close()（OSError/ValueError → cleanup_failed、成功で exited = True）; cleanup_failed → error_type(f"{label} cleanup failed") from None;
        # error があれば error_type(f"{label} failed") from None
```

---

### Task 1: worktree 準備と audit 行 golden（kernel 化前の writer で固定）

**Files:**
- Create: `docs/superpowers/plans/2026-09-04-broker-kernel-6-2-runtime-audit.md`
- Create: `tests/container/test_broker_audit_golden.py`

**Interfaces:**
- Consumes: 現行の `HandoverBrokerSession`（commit `4c555fd`）
- Produces: audit 2 行の golden byte（後続 task の回帰網）

- [ ] **Step 1: worktree を作る**

```bash
cd /home/tsu/Projects/agent-container
git status --short   # 空であること
git worktree add .worktrees/feat-broker-kernel-runtime -b feat/broker-kernel-runtime main
cd .worktrees/feat-broker-kernel-runtime
chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json
git rev-parse --short HEAD   # 4c555fd であること（golden の出自）
```

- [ ] **Step 2: plan を repo に写す**

`/home/tsu/.claude/plans/jaunty-skipping-wall.md` の内容を `docs/superpowers/plans/2026-09-04-broker-kernel-6-2-runtime-audit.md` へそのまま保存する。

- [ ] **Step 3: golden test を書く（現行 writer で PASS する test）**

`tests/container/test_broker_audit_golden.py`:

```python
"""Golden audit lines captured from the pre-kernel handover writer.

Generated at commit 4c555fd with HandoverBrokerSession.audit before the
broker kernel owned audit files. Never regenerate these bytes from the
kernel's own output; a mismatch here means the audit line format changed.
"""

from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker import HandoverBrokerSession


GOLDEN_RUN_LABEL = "0123456789abcdef"
GOLDEN_PATH = "/handovers/agent-container/2026-09-04_000000_deadbeef.md"
GOLDEN_AUDIT_BYTES = (
    b'{"timestamp":"2026-09-04T00:00:00.000001+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","operation":"create","status":"ok","stage":"write",'
    b'"path":"/handovers/agent-container/2026-09-04_000000_deadbeef.md"}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000002+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","operation":"create","status":"denied",'
    b'"stage":"authentication"}\n'
)


class HandoverAuditGoldenTest(unittest.TestCase):
    def test_audit_lines_match_the_pre_kernel_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-golden-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            handovers = root / "handovers"
            handovers.mkdir(mode=0o700)
            project = handovers / "agent-container"
            project.mkdir(mode=0o700)
            session = HandoverBrokerSession.create(
                state.resolve(), "agent-container", project.resolve()
            )
            try:
                stamps = iter(
                    [
                        "2026-09-04T00:00:00.000001+00:00",
                        "2026-09-04T00:00:00.000002+00:00",
                    ]
                )
                fixed_now = mock.Mock()
                fixed_now.isoformat.side_effect = lambda: next(stamps)
                with mock.patch(
                    "agent_container.handover_broker.datetime"
                ) as fixed_datetime, mock.patch.object(
                    HandoverBrokerSession,
                    "run_label",
                    new_callable=mock.PropertyMock,
                    return_value=GOLDEN_RUN_LABEL,
                ):
                    fixed_datetime.now.return_value = fixed_now
                    session.audit("ok", stage="write", path=GOLDEN_PATH)
                    session.audit("denied", stage="authentication")

                self.assertEqual(session.audit_file.read_bytes(), GOLDEN_AUDIT_BYTES)
                self.assertEqual(
                    stat.S_IMODE(session.audit_file.stat().st_mode), 0o600
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: 現行 writer で PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_audit_golden -v`
Expected: 1 test, OK。（`timestamp` は `agent_container.handover_broker.datetime` を patch して固定、`run` は `run_label` property を patch して固定。上の byte 列は `4c555fd` で `_write_audit_record` を直接呼んで実測したもの）

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-09-04-broker-kernel-6-2-runtime-audit.md tests/container/test_broker_audit_golden.py
git commit -m "test: pin handover audit lines before extracting the broker kernel audit log

Golden lines generated by the pre-kernel writer at 4c555fd. The audit
extraction that follows must reproduce these bytes exactly.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 2: `broker/audit.py`（TDD）

**Files:**
- Create: `src/agent_container/broker/audit.py`
- Test: `tests/container/test_broker_audit.py`

**Interfaces:**
- Produces: 「Interfaces」節の `AuditLog`

- [ ] **Step 1: failing test を書く**

`tests/container/test_broker_audit.py`:

```python
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.audit import AuditLog


LABEL = "test audit"


class AuditLogTest(unittest.TestCase):
    def test_validate_creates_a_private_empty_file_and_append_writes_ascii_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = AuditLog(path, label=LABEL)

            log.validate()

            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b"")

            log.append({"timestamp": "t1", "status": "ok", "path": "/x"})
            log.append({"timestamp": "t2", "status": "denied"})

            self.assertEqual(
                path.read_bytes(),
                b'{"timestamp":"t1","status":"ok","path":"/x"}\n'
                b'{"timestamp":"t2","status":"denied"}\n',
            )

    def test_append_escapes_non_ascii_and_preserves_key_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            AuditLog(path, label=LABEL).append({"b": "é", "a": 1})
            self.assertEqual(path.read_bytes(), b'{"b":"\\u00e9","a":1}\n')

    def test_rejects_symlink_fifo_directory_and_wrong_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("", encoding="ascii")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "test audit file must be a regular non-symlink file"):
                AuditLog(link, label=LABEL).validate()

            fifo = root / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                AuditLog(fifo, label=LABEL).validate()

            folder = root / "folder"
            folder.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                AuditLog(folder, label=LABEL).validate()

            wrong_mode = root / "wrong-mode"
            wrong_mode.write_text("", encoding="ascii")
            wrong_mode.chmod(0o644)
            with self.assertRaisesRegex(PermissionError, "test audit file must have mode 0600"):
                AuditLog(wrong_mode, label=LABEL).validate()

    def test_opens_with_nonblocking_append_nofollow_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            real_open = os.open
            seen: list[int] = []

            def checked_open(target: object, flags: int, *args: object, **kwargs: object) -> int:
                if Path(str(target)) == path:
                    seen.append(flags)
                return real_open(target, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=checked_open):
                AuditLog(path, label=LABEL).validate()

            self.assertEqual(len(seen), 1)
            for flag in (os.O_WRONLY, os.O_APPEND, os.O_CREAT, os.O_NOFOLLOW, os.O_NONBLOCK):
                self.assertTrue(seen[0] & flag)

    def test_rejects_foreign_owner_and_replacement_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("", encoding="ascii")
            path.chmod(0o600)
            real_fstat = os.fstat

            def foreign_owner(descriptor: int) -> os.stat_result:
                metadata = real_fstat(descriptor)
                values = list(metadata)
                values[4] = os.getuid() + 1
                return os.stat_result(values)

            with mock.patch("os.fstat", side_effect=foreign_owner), self.assertRaisesRegex(
                PermissionError, "test audit file must be owned by the current user"
            ):
                AuditLog(path, label=LABEL).validate()

            real_stat = os.stat

            def replaced(target: object, *args: object, **kwargs: object) -> os.stat_result:
                metadata = real_stat(target, *args, **kwargs)
                if Path(str(target)) == path and kwargs.get("follow_symlinks") is False:
                    values = list(metadata)
                    values[1] = metadata.st_ino + 1
                    return os.stat_result(values)
                return metadata

            with mock.patch("os.stat", side_effect=replaced), self.assertRaisesRegex(
                ValueError, "test audit file changed during validation"
            ):
                AuditLog(path, label=LABEL).validate()

    def test_descriptor_is_closed_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("", encoding="ascii")
            path.chmod(0o644)
            real_close = os.close
            closed: list[int] = []

            def tracked_close(descriptor: int) -> None:
                closed.append(descriptor)
                real_close(descriptor)

            with mock.patch("os.close", side_effect=tracked_close), self.assertRaises(PermissionError):
                AuditLog(path, label=LABEL).validate()

            self.assertEqual(len(closed), 1)

    def test_append_rejects_short_writes_and_leaves_no_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with mock.patch("os.write", return_value=0), self.assertRaisesRegex(
                OSError, "test audit write failed"
            ):
                AuditLog(path, label=LABEL).append({"a": 1})
            self.assertEqual(path.read_bytes(), b"")

    def test_append_retries_partial_writes_and_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            real_write = os.write
            calls: list[int] = []

            def partial(descriptor: int, body: bytes) -> int:
                calls.append(len(body))
                return real_write(descriptor, body[:3])

            with mock.patch("os.write", side_effect=partial), mock.patch("os.fsync") as fsync:
                AuditLog(path, label=LABEL).append({"a": 1})

            self.assertEqual(path.read_bytes(), b'{"a":1}\n')
            self.assertGreater(len(calls), 1)
            fsync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_audit -v`
Expected: `ModuleNotFoundError: No module named 'agent_container.broker.audit'`

- [ ] **Step 3: 実装**

`src/agent_container/broker/audit.py`:

```python
"""Private append-only audit log shared by every broker."""

import json
import os
from pathlib import Path
import stat
from typing import Mapping


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class AuditLog:
    def __init__(self, path: Path, *, label: str) -> None:
        self.path = path
        self.label = label

    def open_descriptor(self) -> int:
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK,
                0o600,
            )
        except OSError:
            raise ValueError(
                f"{self.label} file must be a regular non-symlink file"
            ) from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"{self.label} file must be a regular non-symlink file"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError(f"{self.label} file must have mode 0600")
            if metadata.st_uid != os.getuid():
                raise PermissionError(
                    f"{self.label} file must be owned by the current user"
                )
            try:
                current = os.stat(self.path, follow_symlinks=False)
            except OSError:
                raise ValueError(
                    f"{self.label} file must be a regular non-symlink file"
                ) from None
            if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
                raise ValueError(f"{self.label} file changed during validation")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return descriptor

    def validate(self) -> None:
        os.close(self.open_descriptor())

    def append(self, record: Mapping[str, object]) -> None:
        body = (
            json.dumps(dict(record), ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        descriptor = self.open_descriptor()
        try:
            offset = 0
            while offset < len(body):
                written = os.write(descriptor, body[offset:])
                if written <= 0:
                    raise OSError(f"{self.label} write failed")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
```

- [ ] **Step 4: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_audit -v`
Expected: 8 tests OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/audit.py tests/container/test_broker_audit.py
git commit -m "feat: add the broker kernel audit log

Private append-only JSON lines with the open-time checks the handover
and egress brokers each implemented: O_NOFOLLOW|O_NONBLOCK open,
regular file, mode 0600, current-user owner, dev/ino re-check.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 3: `broker/runtime.py` 資源 helper（TDD）

**Files:**
- Create: `src/agent_container/broker/runtime.py`（この task では helper 5 関数のみ。`SocketBrokerRuntime` は Task 5 で同 file に追記）
- Test: `tests/container/test_broker_runtime.py`（この task では helper の test class のみ。lifecycle の test class は Task 5 で同 file に追記）

**Interfaces:**
- Consumes: `agent_container.broker.capability.CAPABILITY_PATTERN`（6-1）
- Produces: `MAX_UNIX_SOCKET_PATH_BYTES`、`create_private_file`、`allocate_run_dir`、`generate_capability`、`bind_private_listener`、`remove_runtime_artifacts`

- [ ] **Step 1: failing test を書く**

`tests/container/test_broker_runtime.py`:

```python
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.runtime import MAX_UNIX_SOCKET_PATH_BYTES
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts


LABEL = "test broker"


class CreatePrivateFileTest(unittest.TestCase):
    def test_creates_exclusive_private_ascii_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            create_private_file(path, "abc\n", label=LABEL)
            self.assertEqual(path.read_bytes(), b"abc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                create_private_file(path, "again\n", label=LABEL)

    def test_refuses_symlink_target_and_short_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                create_private_file(link, "abc\n", label=LABEL)
            self.assertFalse(target.exists())

            path = root / "short"
            with mock.patch("os.write", return_value=0), self.assertRaisesRegex(
                OSError, "test broker private file write failed"
            ):
                create_private_file(path, "abc\n", label=LABEL)


class AllocateRunDirTest(unittest.TestCase):
    def test_allocates_a_private_hex_named_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_id, run_dir = allocate_run_dir(Path(directory), label=LABEL)
            self.assertRegex(run_id, r"^[0-9a-f]{16}$")
            self.assertEqual(run_dir, Path(directory) / run_id)
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_retries_collisions_then_gives_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aaaaaaaaaaaaaaaa").mkdir(mode=0o700)
            with mock.patch(
                "secrets.token_hex", side_effect=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
            ):
                run_id, run_dir = allocate_run_dir(root, label=LABEL)
            self.assertEqual(run_id, "bbbbbbbbbbbbbbbb")
            self.assertTrue(run_dir.is_dir())

            with mock.patch("secrets.token_hex", return_value="aaaaaaaaaaaaaaaa"), self.assertRaisesRegex(
                FileExistsError, "could not allocate test broker runtime"
            ):
                allocate_run_dir(root, label=LABEL, attempts=3)


class GenerateCapabilityTest(unittest.TestCase):
    def test_generates_43_url_safe_characters(self) -> None:
        capability = generate_capability(label=LABEL)
        self.assertEqual(len(capability), 43)
        self.assertIsNotNone(CAPABILITY_PATTERN.fullmatch(capability))

    def test_rejects_unexpected_token_shape(self) -> None:
        with mock.patch("secrets.token_urlsafe", return_value="short"), self.assertRaisesRegex(
            RuntimeError, "generated test broker capability has invalid format"
        ):
            generate_capability(label=LABEL)


class BindPrivateListenerTest(unittest.TestCase):
    def test_binds_a_private_listening_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            listener = bind_private_listener(path, backlog=4, label=LABEL)
            try:
                metadata = path.lstat()
                self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.connect(str(path))
                finally:
                    client.close()
            finally:
                listener.close()

    def test_rejects_long_paths_and_existing_paths(self) -> None:
        self.assertEqual(MAX_UNIX_SOCKET_PATH_BYTES, 107)
        long_path = Path("/" + "a" * 120)
        with self.assertRaisesRegex(ValueError, "test broker socket path is too long"):
            bind_private_listener(long_path, backlog=4, label=LABEL)
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            path.write_text("replacement", encoding="ascii")
            with self.assertRaisesRegex(FileExistsError, "test broker socket path already exists"):
                bind_private_listener(path, backlog=4, label=LABEL)

    def test_bind_failure_closes_socket_and_only_unlinks_a_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            fake.bind.side_effect = OSError("bind failed")
            with mock.patch("socket.socket", return_value=fake), self.assertRaises(OSError):
                bind_private_listener(path, backlog=4, label=LABEL)
            fake.close.assert_called_once_with()
            self.assertFalse(path.exists())

    def test_chmod_receives_the_path_object(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            with mock.patch("socket.socket", return_value=fake), mock.patch("os.chmod") as chmod:
                self.assertIs(bind_private_listener(path, backlog=7, label=LABEL), fake)
            fake.bind.assert_called_once_with(str(path))
            chmod.assert_called_once_with(path, 0o600)
            fake.listen.assert_called_once_with(7)


class RemoveRuntimeArtifactsTest(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path, Path]:
        run_dir = root / "run"
        run_dir.mkdir(mode=0o700)
        capability = run_dir / "capability"
        capability.write_text("c" * 43 + "\n", encoding="ascii")
        capability.chmod(0o600)
        return run_dir, capability, run_dir / "broker.sock"

    def test_removes_capability_socket_and_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            listener = bind_private_listener(socket_path, backlog=1, label=LABEL)
            listener.close()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_missing_artifacts_are_not_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir(mode=0o700)
            failed = remove_runtime_artifacts(
                capability_path=run_dir / "capability",
                socket_path=run_dir / "broker.sock",
                run_dir=run_dir,
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_refuses_replaced_capability_and_keeps_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            capability.unlink()
            capability.mkdir()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertTrue(capability.is_dir())
            self.assertTrue(run_dir.exists())

    def test_refuses_replaced_socket_but_still_removes_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            socket_path.write_text("replacement", encoding="ascii")
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertFalse(capability.exists())
            self.assertTrue(socket_path.is_file())
            self.assertTrue(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime -v`
Expected: `ModuleNotFoundError: No module named 'agent_container.broker.runtime'`

- [ ] **Step 3: 実装**

`src/agent_container/broker/runtime.py`:

```python
"""Host-side broker runtime: private artifacts and the serve lifecycle."""

import os
from pathlib import Path
import secrets
import socket
import stat

from agent_container.broker.capability import CAPABILITY_PATTERN


MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def create_private_file(path: Path, body: str, *, label: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
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


def allocate_run_dir(
    project_root: Path, *, label: str, attempts: int = 8
) -> tuple[str, Path]:
    for _ in range(attempts):
        run_id = secrets.token_hex(8)
        run_dir = project_root / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise FileExistsError(f"could not allocate {label} runtime")


def generate_capability(*, label: str) -> str:
    capability = secrets.token_urlsafe(32)
    if CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise RuntimeError(f"generated {label} capability has invalid format")
    return capability


def bind_private_listener(
    socket_path: Path, *, backlog: int, label: str
) -> socket.socket:
    if len(os.fsencode(socket_path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise ValueError(f"{label} socket path is too long")
    if socket_path.exists() or socket_path.is_symlink():
        raise FileExistsError(f"{label} socket path already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(backlog)
    except Exception:
        listener.close()
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(metadata.st_mode):
                socket_path.unlink()
        raise
    return listener


def remove_runtime_artifacts(
    *, capability_path: Path, socket_path: Path, run_dir: Path
) -> bool:
    failed = False
    for path, expected_type in (
        (capability_path, stat.S_ISREG),
        (socket_path, stat.S_ISSOCK),
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
            continue
        if not expected_type(metadata.st_mode):
            failed = True
            continue
        try:
            path.unlink()
        except OSError:
            failed = True
    try:
        run_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        failed = True
    return failed
```

- [ ] **Step 4: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_runtime -v`
Expected: 13 tests OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/runtime.py tests/container/test_broker_runtime.py
git commit -m "feat: add the broker kernel runtime artifact helpers

Run directory allocation, capability generation, exclusive private
files, private Unix listeners, and ordered artifact removal, extracted
from the handover session with the same checks and messages.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 4: `HandoverBrokerSession` を `AuditLog` と資源 helper に乗せ替える

**Files:**
- Modify: `src/agent_container/handover_broker.py`

**Interfaces:**
- Consumes: Task 2 の `AuditLog`、Task 3 の 5 helper
- Produces: 従来どおり `HandoverBrokerSession`（`create`、`run_label`、`authorize`、`publication_guard`、`deactivate`、`open_listener`、`audit`、`close`、`__enter__`／`__exit__`、field `project_id`／`project_dir`／`owner_uid`／`run_id`／`run_dir`／`socket_path`／`capability_path`／`audit_file`／`_capability`／`_listener`／`_closed`／`_cleanup_complete`）。module 属性 `os`、`socket`、`secrets`、`datetime` は維持（test の patch 対象）

- [ ] **Step 1: baseline**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_broker_audit_golden -v 2>&1 | tail -3`
Expected: OK（17 + 1）

- [ ] **Step 2: module を書き換える**

`src/agent_container/handover_broker.py` 全体:

```python
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import threading
from typing import Iterator

from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_writer import validate_handover_content
from agent_container.state import ensure_private_directory
from agent_container.state import handover_broker_project_label
from agent_container.state import validate_project_id


_LABEL = "handover broker"
_AUDIT_LABEL = "handover broker audit"
_HANDOVER_FILENAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{6}_[0-9a-f]{8}\.md$"
)
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_AUDIT_STAGES = frozenset(
    {
        "authentication",
        "schema",
        "size",
        "content-policy",
        "filesystem-boundary",
        "write",
        "unavailable",
        "response",
    }
)


def _validate_project_directory(project_dir: Path, project_id: str) -> Path:
    if not project_dir.is_absolute() or project_dir.name != project_id:
        raise ValueError("handover project directory is invalid")
    try:
        resolved = project_dir.resolve(strict=True)
    except OSError:
        raise ValueError("handover project directory is invalid") from None
    if resolved != project_dir or project_dir.is_symlink() or not project_dir.is_dir():
        raise ValueError("handover project directory is invalid")
    return resolved


def _validate_container_path(path: str, project_id: str) -> str:
    if not isinstance(path, str) or "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError("handover broker audit path is invalid")
    parsed = PurePosixPath(path)
    expected_parent = PurePosixPath("/handovers") / project_id
    if (
        not parsed.is_absolute()
        or parsed.parent != expected_parent
        or _HANDOVER_FILENAME.fullmatch(parsed.name) is None
    ):
        raise ValueError("handover broker audit path is invalid")
    return path


@dataclass
class HandoverBrokerSession:
    project_id: str
    project_dir: Path
    owner_uid: int
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _capability: str = field(repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        state_root: Path,
        project_id: str,
        project_dir: Path,
    ) -> "HandoverBrokerSession":
        root = ensure_private_directory(state_root)
        validated_project = validate_project_id(project_id)
        bound_project_dir = _validate_project_directory(
            project_dir,
            validated_project,
        )
        broker_root = ensure_private_directory(root / "handover-broker", create=True)
        audit_root = ensure_private_directory(broker_root / "audit", create=True)
        run_root = ensure_private_directory(broker_root / "r", create=True)
        project_root = ensure_private_directory(
            run_root / handover_broker_project_label(validated_project),
            create=True,
        )
        audit_file = audit_root / "events.jsonl"
        AuditLog(audit_file, label=_AUDIT_LABEL).validate()

        run_id, run_dir = allocate_run_dir(project_root, label=_LABEL)
        try:
            capability = generate_capability(label=_LABEL)
        except RuntimeError:
            shutil.rmtree(run_dir)
            raise
        capability_path = run_dir / "capability"
        try:
            create_private_file(capability_path, capability + "\n", label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
        return cls(
            project_id=validated_project,
            project_dir=bound_project_dir,
            owner_uid=os.getuid(),
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _capability=capability,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(
        self,
        request: HandoverRequest,
        peer_uid: int,
    ) -> tuple[str, str]:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("handover broker session is closed")
            if peer_uid != self.owner_uid:
                raise ValueError("handover broker request is not authorized")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("handover broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("handover broker request is not authorized")
            if request.project_id != self.project_id:
                raise ValueError("handover broker request project is not allowed")
            if request.operation != "create":
                raise ValueError("handover broker request operation is not allowed")
        return validate_handover_content(request.title, request.body)

    @contextmanager
    def publication_guard(self) -> Iterator[None]:
        with self._lifecycle_lock:
            if self._closed:
                raise OSError("handover publication is unavailable")
            yield

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("handover broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        self._listener = listener
        return listener

    def audit(self, status: str, *, stage: str, path: str = "") -> None:
        if self._closed:
            raise ValueError("handover broker session is closed")
        if status not in _AUDIT_STATUSES:
            raise ValueError("handover broker audit status is invalid")
        if stage not in _AUDIT_STAGES:
            raise ValueError("handover broker audit stage is invalid")
        if status == "ok":
            path = _validate_container_path(path, self.project_id)
        elif path:
            raise ValueError("handover broker audit path is invalid")

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_label,
            "project": self.project_id,
            "operation": "create",
            "status": status,
            "stage": stage,
        }
        if path:
            record["path"] = path
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
            raise ValueError("handover broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "HandoverBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
```

削除したもの: `import json`、`import stat`、`_CAPABILITY`、`_MAX_UNIX_SOCKET_PATH_BYTES`、`_NOFOLLOW`、`_NONBLOCK`、`_create_private_file`、`_open_audit_file`、`_write_audit_record`。**`import os`、`import socket`、`import secrets`、`from datetime import datetime, timezone` は残す**（`os.getuid` と型注釈、`secrets.compare_digest`、`datetime.now` を使うので ruff の F401 にもならず、test の patch 対象として存在し続ける）。

振る舞い保存の根拠:
- 旧 `create` は `_open_audit_file` の descriptor を即 close していた → `AuditLog.validate()` は同じ open→検証→close。同 flag、同 message（`_AUDIT_LABEL` = `"handover broker audit"` + `" file must be ..."`）。
- 旧 run dir 割り当て: `token_hex(8)` を 8 回まで、失敗で `FileExistsError("could not allocate handover broker runtime")` → 同一。
- 旧 capability: `token_urlsafe(32)` を regex 検証、不正なら `shutil.rmtree(run_dir)` 後に `RuntimeError` → `generate_capability` が RuntimeError を出し、session 側で rmtree して再送出（順序同一）。
- 旧 `open_listener`: `_closed`／二重 open の ValueError を先に、path 長→既存→bind→chmod(Path)→listen、失敗時 close と S_ISSOCK なら unlink → `bind_private_listener` が同一。
- 旧 `close`: deactivate → listener close → capability(S_ISREG)→socket(S_ISSOCK) の順に unlink → rmdir → 失敗なら `ValueError("handover broker cleanup failed")` → `remove_runtime_artifacts` が同一順序で bool を返す。
- 旧 `audit`: record 構築と timestamp は session 側のまま。`AuditLog.append` は旧 `_write_audit_record` と同じ serialization。

- [ ] **Step 3: 既存 test と golden を実行**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_broker_audit_golden tests.container.test_handover_broker_runtime tests.container.test_handover_broker_transport -v 2>&1 | tail -5`
Expected: 全件 OK。`git diff --stat main -- tests/` に既存 handover test file が現れないこと。

- [ ] **Step 4: lint**

Run: `bin/lint`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/handover_broker.py
git commit -m "refactor: back the handover session with the broker kernel

Audit file validation and appends, run directory allocation,
capability generation, the private listener, and artifact removal
now come from agent_container.broker. Authorization, audit record
construction, and the lifecycle lock stay in the session. The
session tests and audit golden are unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 5: `broker/readiness.py` と `SocketBrokerRuntime`（TDD）

**Files:**
- Create: `src/agent_container/broker/readiness.py`
- Modify: `src/agent_container/broker/runtime.py`（`SocketBrokerRuntime` を末尾に追記）
- Test: `tests/container/test_broker_readiness.py`（Create）、`tests/container/test_broker_runtime.py`（lifecycle の test class を末尾に追記）

**Interfaces:**
- Consumes: Task 3 の `runtime.py`
- Produces: `ReadinessGate`、`AlwaysReady`、`SocketBrokerRuntime`（「Interfaces」節どおり）

- [ ] **Step 1: readiness の failing test**

`tests/container/test_broker_readiness.py`:

```python
import unittest

from agent_container.broker.readiness import AlwaysReady
from agent_container.broker.readiness import ReadinessGate


class AlwaysReadyTest(unittest.TestCase):
    def test_is_a_gate_that_is_always_open(self) -> None:
        gate: ReadinessGate = AlwaysReady()
        gate.register(1234)
        self.assertTrue(gate.is_ready())
        self.assertTrue(gate.wait())
        self.assertTrue(gate.wait(timeout=0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: lifecycle の failing test を `tests/container/test_broker_runtime.py` の末尾（`if __name__` の前）に追記**

先頭の import に次を追加する:

```python
from io import BytesIO
import struct
import threading

from agent_container.broker.runtime import SocketBrokerRuntime
```

追記する test:

```python
class RuntimeError_(Exception):
    pass


class FakeStream:
    def __init__(self) -> None:
        self.closed = False
        self.outgoing = BytesIO()

    def read(self, size: int) -> bytes:
        return b""

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, peer_uid: int) -> None:
        self.peer_uid = peer_uid
        self.timeout: float | None = None
        self.credential_calls: list[tuple[int, int, int]] = []
        self.stream = FakeStream()
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.credential_calls.append((level, option, size))
        return struct.pack("3i", 1234, self.peer_uid, 5678)

    def makefile(self, *_: object, **__: object) -> FakeStream:
        return self.stream

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


class FakeListener:
    def __init__(self, clients: tuple[FakeClient, ...] = ()) -> None:
        self.clients = list(clients)
        self.timeout: float | None = None
        self.closed = False
        self.accepts = 0
        self._wake = threading.Event()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self) -> tuple[FakeClient, None]:
        self.accepts += 1
        if self.clients:
            return self.clients.pop(0), None
        self._wake.wait(0.01)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True
        self._wake.set()


class ManualGate:
    def __init__(self) -> None:
        self.opened = threading.Event()
        self.registered: list[int] = []

    def register(self, peer: int) -> None:
        self.registered.append(peer)

    def wait(self, timeout: float | None = None) -> bool:
        return self.opened.wait(timeout)

    def is_ready(self) -> bool:
        return self.opened.is_set()


def make_runtime(
    listener: FakeListener,
    handler=None,
    *,
    readiness=None,
    open_listener=None,
    close=None,
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

    options = {}
    if readiness is not None:
        options["readiness"] = readiness
    runtime = SocketBrokerRuntime(
        label="test broker",
        thread_name="test-broker",
        open_listener=open_listener or default_open,
        handler=handler or (lambda stream, peer_uid: 0),
        deactivate=deactivate,
        close=close or default_close,
        error_type=RuntimeError_,
        backlog=4,
        listener_timeout=0.2,
        client_timeout=30,
        **options,
    )
    return runtime, calls


class SocketBrokerRuntimeTest(unittest.TestCase):
    def test_start_opens_listener_and_runs_a_daemon_thread_until_stop(self) -> None:
        listener = FakeListener()
        runtime, calls = make_runtime(listener)

        runtime.start()
        try:
            self.assertEqual(calls["open"], 1)
            self.assertEqual(calls["backlog"], 4)
            self.assertEqual(listener.timeout, 0.2)
            self.assertIsNotNone(runtime.thread)
            self.assertTrue(runtime.thread.is_alive())
            self.assertTrue(runtime.thread.daemon)
            self.assertEqual(runtime.thread.name, "test-broker")
        finally:
            runtime.stop(join_timeout=2)

        self.assertTrue(listener.closed)
        self.assertTrue(runtime.exited)
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)
        self.assertFalse(runtime.thread.is_alive())

    def test_handler_receives_stream_and_peer_uid_and_loop_continues(self) -> None:
        clients = (FakeClient(1010), FakeClient(2020))
        listener = FakeListener(clients)
        handled = threading.Event()
        seen: list[int] = []

        def handler(stream: object, peer_uid: int) -> int:
            seen.append(peer_uid)
            if len(seen) == 2:
                handled.set()
            return 0

        runtime, _ = make_runtime(listener, handler)
        runtime.start()
        try:
            self.assertTrue(handled.wait(1))
        finally:
            runtime.stop(join_timeout=2)

        self.assertEqual(seen, [1010, 2020])
        for client in clients:
            self.assertEqual(
                client.credential_calls, [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]
            )
            self.assertEqual(client.timeout, 30)
            self.assertTrue(client.stream.closed)
            self.assertTrue(client.closed)

    def test_start_failure_closes_listener_and_session_and_raises_fixed_message(self) -> None:
        listener = FakeListener()

        def failing_open(backlog: int) -> FakeListener:
            raise OSError("private-start-marker")

        runtime, calls = make_runtime(listener, open_listener=failing_open)
        with self.assertRaises(RuntimeError_) as raised:
            runtime.start()
        self.assertEqual(str(raised.exception), "test broker failed to start")
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
        with self.assertRaises(RuntimeError_):
            runtime.start()

    def test_start_failure_with_failing_close_allows_retry_through_stop(self) -> None:
        listener = FakeListener()
        close_calls = 0

        def flaky_close() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise ValueError("private-cleanup-marker")

        class StartFailureThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.join_calls = 0

            def start(self) -> None:
                raise RuntimeError("private-thread-start-marker")

            def join(self, timeout: float) -> None:
                self.join_calls += 1

            def is_alive(self) -> bool:
                return False

        thread = StartFailureThread()
        runtime, calls = make_runtime(listener, close=flaky_close)
        with mock.patch("threading.Thread", return_value=thread):
            with self.assertRaises(RuntimeError_) as raised:
                runtime.start()
        self.assertEqual(str(raised.exception), "test broker failed to start")
        self.assertNotIn("private", str(raised.exception))
        self.assertTrue(listener.closed)
        self.assertFalse(runtime.exited)
        self.assertIsNone(runtime.thread)

        runtime.stop(join_timeout=2)
        runtime.stop(join_timeout=2)
        self.assertEqual(thread.join_calls, 0)
        self.assertEqual(close_calls, 2)
        self.assertTrue(runtime.exited)

    def test_handler_exception_is_captured_and_reported_after_cleanup(self) -> None:
        client = FakeClient(os.getuid())
        listener = FakeListener((client,))
        failed = threading.Event()

        def fail(stream: object, peer_uid: int) -> int:
            failed.set()
            raise RuntimeError("private-handler-marker")

        runtime, calls = make_runtime(listener, fail)
        runtime.start()
        self.assertTrue(failed.wait(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")
        self.assertNotIn("private-handler-marker", str(raised.exception))
        self.assertEqual(calls["close"], 1)
        self.assertTrue(listener.closed)
        self.assertTrue(runtime.exited)

    def test_stop_timeout_defers_close_until_the_thread_can_be_joined(self) -> None:
        listener = FakeListener()

        class StuckThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.join_timeouts: list[float] = []
                self.alive = True

            def start(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                self.join_timeouts.append(timeout)

            def is_alive(self) -> bool:
                return self.alive

        thread = StuckThread()
        runtime, calls = make_runtime(listener)
        with mock.patch("threading.Thread", return_value=thread):
            runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(thread.join_timeouts, [2])
        self.assertTrue(listener.closed)
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 0)

        thread.alive = False
        runtime.stop(join_timeout=2)
        self.assertEqual(thread.join_timeouts, [2, 2])
        self.assertEqual(calls["deactivate"], 2)
        self.assertEqual(calls["close"], 1)
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)

    def test_cleanup_failure_is_reported_and_retryable(self) -> None:
        listener = FakeListener()
        close_calls = 0

        def flaky_close() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise OSError("private-cleanup-marker")

        runtime, _ = make_runtime(listener, close=flaky_close)
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker cleanup failed")
        self.assertFalse(runtime.exited)
        runtime.stop(join_timeout=2)
        self.assertTrue(runtime.exited)
        self.assertEqual(close_calls, 2)

    def test_readiness_gate_blocks_accept_until_opened(self) -> None:
        client = FakeClient(os.getuid())
        listener = FakeListener((client,))
        gate = ManualGate()
        handled = threading.Event()
        runtime, _ = make_runtime(
            listener, lambda stream, peer_uid: handled.set(), readiness=gate
        )
        runtime.start()
        try:
            self.assertFalse(handled.wait(0.1))
            self.assertEqual(listener.accepts, 0)
            gate.opened.set()
            self.assertTrue(handled.wait(1))
        finally:
            runtime.stop(join_timeout=2)

    def test_readiness_gate_failure_is_reported_as_runtime_failure(self) -> None:
        listener = FakeListener()

        class ClosedGate:
            def register(self, peer: int) -> None:
                pass

            def wait(self, timeout: float | None = None) -> bool:
                return False

            def is_ready(self) -> bool:
                return False

        runtime, _ = make_runtime(listener, readiness=ClosedGate())
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")
        self.assertEqual(listener.accepts, 0)
```

- [ ] **Step 3: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_readiness tests.container.test_broker_runtime -v 2>&1 | tail -5`
Expected: readiness は `ModuleNotFoundError: No module named 'agent_container.broker.readiness'`、runtime は `ImportError: cannot import name 'SocketBrokerRuntime'`

- [ ] **Step 4: 実装**

`src/agent_container/broker/readiness.py`:

```python
"""Readiness gate a broker runtime waits on before accepting connections."""

from typing import Protocol


class ReadinessGate(Protocol):
    def register(self, peer: int) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def is_ready(self) -> bool: ...


class AlwaysReady:
    def register(self, peer: int) -> None:
        return None

    def wait(self, timeout: float | None = None) -> bool:
        return True

    def is_ready(self) -> bool:
        return True
```

`src/agent_container/broker/runtime.py` の import を次に変え、末尾に `SocketBrokerRuntime` を追記:

```python
from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import threading
from typing import Any
from typing import Callable

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.readiness import AlwaysReady
from agent_container.broker.readiness import ReadinessGate
```

```python
_PEER_CREDENTIAL_BYTES = 12


@dataclass
class SocketBrokerRuntime:
    label: str
    thread_name: str
    open_listener: Callable[[int], Any]
    handler: Callable[[Any, int], object]
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = field(default_factory=AlwaysReady)
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    thread: Any | None = field(default=None, init=False)
    listener: Any | None = field(default=None, init=False, repr=False)
    error: BaseException | None = field(default=None, init=False, repr=False)
    exited: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        if self.thread is not None or self.exited:
            raise self.error_type(f"{self.label} failed to start")
        listener: Any | None = None
        try:
            listener = self.open_listener(self.backlog)
            listener.settimeout(self.listener_timeout)
            self.listener = listener
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
            if not self.readiness.wait():
                raise self.error_type(f"{self.label} readiness gate failed")
            while not self.stop_event.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                with client:
                    client.settimeout(self.client_timeout)
                    credentials = client.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        _PEER_CREDENTIAL_BYTES,
                    )
                    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
                    stream = client.makefile("rwb", buffering=0)
                    try:
                        self.handler(stream, peer_uid)
                    finally:
                        stream.close()
        except BaseException as error:
            self.error = error

    def stop(self, *, join_timeout: float) -> None:
        if self.exited:
            return
        self.stop_event.set()
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

注意: `threading.Thread(...)` は **module 属性参照** のまま書く（`from threading import Thread` にしない）。既存の handover runtime test が `agent_container.handover_broker_runtime.threading.Thread` を patch し、それは `threading.Thread` の global patch なので kernel にも届く。

- [ ] **Step 5: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_readiness tests.container.test_broker_runtime -v 2>&1 | tail -5`
Expected: readiness 1、runtime 13 + 9 = 22、全件 OK。

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image 2>&1 | tail -3`
Expected: 17 OK

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/broker/readiness.py src/agent_container/broker/runtime.py tests/container/test_broker_readiness.py tests/container/test_broker_runtime.py
git commit -m "feat: add the broker kernel serve lifecycle and readiness gate

SocketBrokerRuntime owns the listener thread, accept loop, peer
credential lookup, stop ordering, and error capture the brokers each
implemented; a ReadinessGate seam (AlwaysReady by default) runs before
the first accept. No broker uses it yet.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 6: `HandoverBrokerRuntime` を `SocketBrokerRuntime` に乗せ替える

**Files:**
- Modify: `src/agent_container/handover_broker_runtime.py`

**Interfaces:**
- Consumes: Task 5 の `SocketBrokerRuntime`
- Produces: 従来どおり `HandoverBrokerRuntimeError`、`HandoverRuntimeMount`、`HandoverBrokerRuntime`（`create`、`__enter__`、`__exit__`、属性 `session`、`_thread`、`_stop`）。module 定数 `_LISTENER_TIMEOUT_SECONDS`、`_CLIENT_TIMEOUT_SECONDS`、`_STOP_TIMEOUT_SECONDS`、`_LISTENER_BACKLOG` と module 属性 `threading`、`handle_handover_connection`、`HandoverBrokerSession` は維持（test の patch 対象）

- [ ] **Step 1: baseline**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_runtime -v 2>&1 | tail -3`
Expected: OK（13）

- [ ] **Step 2: module を書き換える**

`src/agent_container/handover_broker_runtime.py` 全体:

```python
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any
from typing import BinaryIO

from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_transport import handle_handover_connection
from agent_container.state import StateLayout


_LISTENER_TIMEOUT_SECONDS = 0.2
_CLIENT_TIMEOUT_SECONDS = 30
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 4


class HandoverBrokerRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class HandoverRuntimeMount:
    run_dir: Path

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "broker.sock"

    @property
    def capability_path(self) -> Path:
        return self.run_dir / "capability"


@dataclass
class HandoverBrokerRuntime(AbstractContextManager[HandoverRuntimeMount]):
    session: HandoverBrokerSession
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="handover broker",
            thread_name="handover-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            handler=self._handle,
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=HandoverBrokerRuntimeError,
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
        )

    @classmethod
    def create(
        cls,
        layout: StateLayout,
        project_dir: Path,
    ) -> "HandoverBrokerRuntime":
        session = HandoverBrokerSession.create(
            layout.root,
            layout.project_id,
            project_dir,
        )
        return cls(session)

    def _handle(self, stream: BinaryIO, peer_uid: int) -> int:
        return handle_handover_connection(self.session, stream, peer_uid)

    @property
    def _thread(self) -> Any | None:
        return self._runtime.thread

    @property
    def _stop(self) -> threading.Event:
        return self._runtime.stop_event

    def __enter__(self) -> HandoverRuntimeMount:
        self._runtime.start()
        return HandoverRuntimeMount(self.session.run_dir)

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
```

削除したもの: `import socket`、`import struct`、`_PEER_CREDENTIAL_BYTES`、旧 `_serve`、旧 `_stop`／`_thread`／`_listener`／`_error`／`_exited` field。**`import threading` は残す**（`_stop` の型注釈で使い、`agent_container.handover_broker_runtime.threading.Thread` の patch 対象として存在し続ける）。

振る舞い保存の根拠（既存 test 13 件との対応）:
- `test_enter_returns_only_after_listener_and_daemon_thread_are_running`: `open_listener(backlog=4)`、`settimeout(0.2)`、daemon thread、`mount == HandoverRuntimeMount(session.run_dir)`、exit で listener close と `close_calls == 1`。
- `test_reads_peer_credentials_and_continues_after_denied_connection`: `handle_handover_connection` は module global を **呼び出し時に** 参照する `_handle` 経由なので patch が効く。`SO_PEERCRED` の 3 引数、client timeout 30、stream close、`with client`。
- `test_start_failure_*`: kernel `start` の失敗経路は旧 `__enter__` と同一（listener close → `close()` を試み、成功時だけ `exited=True` → `"handover broker failed to start"` from None）。`test_thread_start_and_first_cleanup_failure_still_allow_safe_retry` は `threading.Thread` を global patch し `start()` が raise → `thread` は None のまま → 後の `__exit__` で `join` が呼ばれない（`join_calls == 0`）。
- `test_handler_exception_is_captured_and_cleanup_precedes_fixed_error` / `test_handler_failure_during_shutdown_*`: `error` を capture して `stop` の最後に `"handover broker failed"`。
- `test_stop_timeout_defers_cleanup_until_worker_can_be_joined`: `stop_event.set → deactivate → listener close → join(timeout=2) → alive なら "did not stop"（close は呼ばない）`、再 exit で `deactivate` 2 回目、`close` 1 回、3 回目は no-op。
- `test_authorized_delayed_operation_cannot_publish_after_exit_timeout`: `_STOP_TIMEOUT_SECONDS` を `__exit__` 直前に 0 へ patch → `__exit__` が module global を読んで `stop(join_timeout=0)` に渡す。`runtime._thread.join(1)` は property 経由で kernel の thread に届く。
- `test_exit_invalidates_capability_and_new_runtime_does_not_reuse_it`: `create` と `with` の経路は不変。

- [ ] **Step 3: 既存 test を実行（変更なしで PASS すること）**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_runtime -v`
Expected: 13 件 OK。1 件でも FAIL したら **test を変えずに** 実装側を直す。直せなければ止めて報告。

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_handover_broker_transport tests.container.test_handover_broker_client tests.container.test_handover_broker_protocol tests.container.test_broker_frame_golden tests.container.test_broker_audit_golden tests.container.test_agentctl tests.container.test_podman 2>&1 | tail -3`
Expected: OK

Run: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_handover_broker_socket -v 2>&1 | tail -5`
Expected: OK（実 UNIX socket で client → kernel runtime → session → response を往復）

Run: `bin/lint`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add src/agent_container/handover_broker_runtime.py
git commit -m "refactor: back the handover runtime with the broker kernel

The listener thread, accept loop, peer credential lookup, stop
ordering, and error capture now come from SocketBrokerRuntime. The
module keeps its constants, patch points, and _thread/_stop views so
the existing runtime tests run unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 7: docs と全体検証

**Files:**
- Modify: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: spec の `broker/runtime.py` 行と `broker/audit.py` 行を実装に合わせる**

「構成」節の表で、`| \`broker/runtime.py\` |` の行の 責務 cell を次に置き換える:

```
2層で構成する。**資源 helper**: `create_private_file(path, body, *, label)`（`O_CREAT|O_EXCL|O_NOFOLLOW`、mode `0600`、ascii、fsync）、`allocate_run_dir(project_root, *, label, attempts=8)`（`token_hex(8)`、mode `0700`、衝突は再試行）、`generate_capability(*, label)`（`token_urlsafe(32)`、`CAPABILITY_PATTERN`検証）、`bind_private_listener(socket_path, *, backlog, label)`（path長107 byte以下、既存path／symlink拒否、bind、`chmod 0600`、listen、失敗時はsocketだけunlink）、`remove_runtime_artifacts(*, capability_path, socket_path, run_dir) -> bool`（capability(S_ISREG)→socket(S_ISSOCK)→rmdirの固定順序、型不一致は残して失敗を返す）。**lifecycle**: `SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, readiness=AlwaysReady(), backlog, listener_timeout, client_timeout)`。`start()`はlistenerを開きdaemon threadでaccept loopを回す。loopは`readiness.wait()`が真を返すまでacceptしない。接続毎に`settimeout`、`SO_PEERCRED`でpeer uidを取り、`handler(stream, peer_uid)`を呼ぶ。`stop(join_timeout=...)`は`stop_event.set → deactivate → listener close → join → close`の順で、`did not stop`（closeせず再試行可能）、`cleanup failed`、handler例外の`failed`を`error_type`で報告する。brokerはsession（authorize・audit record・lock）を保持し、`open_listener`／`deactivate`／`close`をcallableで渡す。各brokerは`run_dir`から自分のMount型を作る。`concurrency="thread"`（egress）は6-3で追加する。
```

`| \`broker/audit.py\` |` の行の 責務 cell を次に置き換える:

```
`AuditLog(path, *, label)`。`open_descriptor()`は`O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_NONBLOCK`で開き、通常file・mode `0600`・実行user所有・`os.stat(follow_symlinks=False)`とのdev／ino一致を検証する（失敗はdescriptorを閉じてから`ValueError`／`PermissionError`）。`validate()`は開いて閉じるだけ、`append(record)`は`json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"`をasciiで追記しfsyncする。stage 1ではrecordのkeyもtimestampの形式も呼び出し側が決める。handoverとegressの実装はlabel以外同一だったので、両方をこの1つで賄う。
```

「構成」節の設計入力の段落（「6-1の実装と最終reviewで判明した」で始まる段落）の直後に、次の段落を追加する:

```
6-2では`handover_broker_transport.py`を据え置いた。transportのhost側`_read_exact`は`_RequestFailure("schema")`、長さ検査は`_RequestFailure("size")`を出し、この区別がauditの`stage`に入る。kernelの`read_frame`へ単純に置き換えると`size`が`schema`に畳まれてaudit行が変わるため、transportのkernel化は例外種別の口を含めてstage 2で扱う。
```

- [ ] **Step 2: CHANGELOG**

`## [Unreleased]` → `### Added` の末尾に追加:

```markdown
- Phase 6 stage 1の2番目の乗せ替えとして、共通broker kernelに`audit`（private append-only audit log）、`readiness`（`ReadinessGate`と`AlwaysReady`）、`runtime`（run directory・capability・private socketの生成回収と、accept loop・stop・error captureの`SocketBrokerRuntime`）を追加し、handover brokerのsessionとruntimeをその上に移しました。audit行、error message、停止順序、既存のhandover testは変更していません。kernel化前のwriterで生成したaudit行のgolden fixtureを`tests/container/test_broker_audit_golden.py`に固定しました。
```

- [ ] **Step 3: 全体検証**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | grep -E "^Ran|^OK|FAILED"
PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | grep -E "^Ran|^OK|FAILED"
bin/lint
git diff --stat main -- tests/
git diff --stat main -- src/
```
Expected: container 1027 + 31（audit golden 1、audit 8、readiness 1、runtime 22）= 1058 OK、codex 44 OK、lint pass。`tests/` の差分は `test_broker_audit_golden.py`、`test_broker_audit.py`、`test_broker_readiness.py`、`test_broker_runtime.py` の 4 file のみ。`src/` の差分は `broker/audit.py`、`broker/readiness.py`、`broker/runtime.py`、`handover_broker.py`、`handover_broker_runtime.py` の 5 file のみ。

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-09-04-broker-kernel-design.md CHANGELOG.md
git commit -m "docs: record the broker kernel 6-2 landing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

push と PR は controller が最終 review の後に行う。

---

## Verification（end-to-end）

1. golden: `test_broker_audit_golden.py` が kernel 化の前後で同じ audit byte を要求する（Task 1 で現行 writer に対して PASS、Task 4 以降も PASS）。`test_broker_frame_golden.py`（6-1）も引き続き PASS。
2. 既存 test 不変: `git diff --stat main -- tests/` に handover の既存 7 file が現れない。
3. 実 socket: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1` で `tests/integration/test_handover_broker_socket.py` が client → kernel runtime → session → writer → response を往復する。
4. image contract: `tests/container/test_image.py` が新 module を image source set に含める。
5. CI: Unit tests と Podman integration。
6. 実 host smoke は 6-6 でまとめて行う。

## Self-review 記録

- Spec coverage（6-2 の範囲）: audit.py → Task 2、runtime.py 資源 helper → Task 3、session 乗せ替え → Task 4、readiness.py と `SocketBrokerRuntime`（readiness seam、fail-closed cleanup の固定順序）→ Task 5、runtime 乗せ替え → Task 6、audit 行同一性 → Task 1 golden、spec 更新 → Task 7。transport 据え置きは Context で決定し Task 7 で spec に記録。
- 型の一貫性: `AuditLog(path, *, label)`／`validate()`／`append(record)`（Task 2）を Task 4 で同名で使う。`allocate_run_dir(project_root, *, label, attempts)`、`generate_capability(*, label)`、`create_private_file(path, body, *, label)`、`bind_private_listener(socket_path, *, backlog, label)`、`remove_runtime_artifacts(*, capability_path, socket_path, run_dir) -> bool`（Task 3）を Task 4 で同名同引数で使う。`SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, readiness, backlog, listener_timeout, client_timeout)`、`start()`、`stop(*, join_timeout)`、属性 `thread`／`stop_event`／`listener`／`error`／`exited`（Task 5）を Task 6 で同名で使う。
- Placeholder: なし。
- 既知の注意: Task 5 の test は `mock.patch("threading.Thread")` と `mock.patch("socket.socket")` を使う。これらは global patch なので test 内で `with` の範囲を最小にしてある。
