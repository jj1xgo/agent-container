# Phase 6-4 GitHub Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub broker の挙動を保存し、frame/chunk、accept、run directory、audit 書き込みを共通 kernel の部品へ移す。

**Architecture:** JSON decode の互換 callback、chunk の read callback、既存の TextIO audit opener を GitHub 側に残す。共通 kernel は framing、accept iteration、audit write sequence を提供し、GitHub の lifecycle/cleanup/capability policy を所有しない。完全統一と保証の変更は stage 2 の別設計で扱う。

**Tech Stack:** Python 3.11 以降、標準ライブラリ、unittest、Ruff。Linux/rootless Podman の既存 CI。

**Spec:** `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` の「6-4 の承認済み互換性範囲」。調査根拠は `docs/superpowers/plans/2026-09-05-broker-kernel-6-4-investigation.md`。

## Global Constraints

- **kernelはbroker固有moduleを一切importしない。**
- **`StateLayout`**、**Mount型と`podman.py`**、**wire形式**、**audit行**を変更しない。
- `PROTOCOL_VERSION`は1のまま。
- 該当brokerの既存test（protocol、runtime、broker、client、transport）を**変更せずに**passする。
- `bin/lint`、CIのUnit testsとPodman integrationがpassする。
- 既存brokerのbugを見つけても、kernel化PRには混ぜない。
- Python floor は README の 3.11。新規依存なし。実 host smoke は 6-6 の既存手順。
- 2026-09-05 の利用者承認: 互換処理を GitHub 側に残し、完全統一を stage 2 に送る。これは仕様の範囲変更の承認であり、既存 bug 修正の承認ではない。
- 基準 commit: `a69bb780dc61f3f0f50c92f668f8686837280f12`。golden は必ずこの commit のコードで生成する。将来の checkout の encoder で期待値を更新しない。

## 変更境界

| File | 責務と変更 |
| --- | --- |
| `src/agent_container/broker/frame.py` | JSON decoder callback、chunk framing の追加。既存 encode/read/write の既定動作は保存 |
| `src/agent_container/github_broker_protocol.py` | request framing/schema と chunk を委譲。JSON decode callback、typed validation、request encoder、response codec、`_read_exact` は互換処理 |
| `src/agent_container/broker/runtime.py` | `accept_clients` を抽出し、既存 kernel loop も利用 |
| `src/agent_container/github_broker_runtime.py` | `_serve` の accept 部分のみ委譲。policy/factory/start/stop と stream 所有権は保存 |
| `src/agent_container/broker/audit.py` | 検査済み TextIO に対する `append_text_record` を追加。`AuditLog` は変更しない |
| `src/agent_container/github_broker.py` | `allocate_run_dir` と `append_text_record` を利用。capability生成・作成、audit opener、listener bind、cleanup は保存 |
| `tests/container/broker_github_golden_support.py` | 固定入力の観測器。golden生成と現在値収集で同じ入力を使う |
| `tests/container/test_broker_github_golden.py` | 静的 JSON fixture との wire/audit 比較 |
| `tests/fixtures/broker_github_golden.json` | 基準 commit から生成した静的期待値 |
| `tests/container/test_github_broker_compatibility.py` | 例外、stream、lifecycle の追加 characterization |
| `tests/container/test_broker_github_primitives.py` | 新 kernel API の独立境界テスト |
| `CHANGELOG.md`、spec、roadmap、計画 | 実際の抽出範囲、残置、検証状態 |

`github_broker_transport.py`、`egress_adapter.py`、`broker/capability.py`、`broker/readiness.py`、`podman.py`、`agentctl.py`、`state.py`、既存 GitHub test files は編集しない。新しい public class、lifecycle compatibility mode、capability validation flag は導入しない。

## Private surface（tests 全体の検索済み一覧）

実装開始時にも `rg -n 'runtime\._|session\._' tests` と `rg -n 'UploadPackBrokerRuntime|_serve|_thread|_stop|_error' tests` を実行する。integration を除外しない。

| 保存する名 | 現在の利用先 |
| --- | --- |
| `BrokerSession._closed` | `test_github_broker.py:31` |
| `BrokerSession._capability` | `test_github_broker.py:38,80,138,191`、`test_github_broker_transport.py:386` |
| `BrokerSession._seen_sequences` | `test_github_broker.py:95` |
| `BrokerSession._listener` | runtime `__exit__` の production consumer |
| `UploadPackBrokerRuntime._stop/_thread/_error/_serve` | field/method 自体を保存。既存 test の直接参照は見つからなかったが削除しない |
| `github_broker_runtime._validate_policy_parent_identity` | `test_github_broker_runtime.py:464`。変更対象外 |
| `github_broker.socket.socket`、`github_broker.os.chmod` | 既存 broker test の patch target |
| `github_broker_transport.read_broker_capability/validate_broker_socket` | 既存 transport test の patch target |
| `UploadPackBrokerRuntime.create`、transport constructors | factory test、agentctl test、socket integration |

## Task 1: 旧実装の golden と互換性を固定する

**Files:** Create 上記 support、golden test、fixture、compatibility test。

**Interfaces:** Consumes 既存 `BrokerRequest`/`BrokerResponse`/`BrokerSession.audit`。Produces `collect_golden() -> dict[str, object]` と基準 commit の静的 fixture。後続 task は期待値を変更しない。

- [ ] **Step 1: isolated worktree と基準 tree を確認する。** 実装開始時に using-git-worktrees を適用し、`feat/broker-kernel-github` を現在の main から作る。既存 worktree/dotfile と `.git/config.lock` は保全する。計画作成時に source/tests は未変更。

計画・investigation の新規 2 文書と spec/roadmap の変更は、計画作成時点では main の未コミット差分である。新 worktree へは自動で含まれない。実装前にこの 4 文書だけを内容照合付きで移し、他の未追跡 dotfile を巻き込まず計画用 commit に保存する。元 workspace のコピーを消す必要はない。Task 1 のコード変更とは commit を分ける。

```sh
git status --short --branch
git worktree list
git diff a69bb780dc61f3f0f50c92f668f8686837280f12 -- src/agent_container/github_broker.py src/agent_container/github_broker_protocol.py src/agent_container/github_broker_runtime.py
```

差があれば基準を勝手に更新せず、その変更を比較して適用可否を判断する。

- [ ] **Step 2: support を次の内容で作る。** input は全て合成値。audit は固定 run_id と clock、通常の安全な一時 directory を使う。

```python
import io
import json
from pathlib import Path
import tempfile
from unittest import mock

from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_protocol import BrokerRequest, BrokerResponse
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import write_chunk_stream


def collect_golden() -> dict[str, object]:
    payloads = {
        "git-upload-pack": {"repository": "demo/repo"},
        "git-receive-pack": {"repository": "demo/repo"},
        "pr-create": {"base": "main", "head": "feat/demo", "title": "日本語", "body": "line\n2"},
        "pr-view": {"number": 7},
        "pr-checks": {"number": 7},
        "issue-list": {},
        "issue-view": {"number": 8},
    }
    requests = {
        op: encode_request_frame(BrokerRequest(1, "A" * 43, "demo", 123, op, payload)).hex()
        for op, payload in payloads.items()
    }
    responses = {
        status: encode_response_frame(BrokerResponse(1, status)).hex()
        for status in ("ok", "denied", "error")
    }
    stream = io.BytesIO()
    transferred = write_chunk_stream(stream, (b"abc", b"\x00\xff"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        policy = BrokerPolicy.create(
            project_id="demo", repository="demo/repo", default_branch="main",
            protected_branches=("main",),
        )
        session = BrokerSession(
            policy=policy, run_id="0123456789abcdef", run_dir=root,
            socket_path=root / "broker.sock", capability_path=root / "capability",
            audit_file=root / "events.jsonl", _capability="A" * 43,
        )
        with mock.patch("agent_container.github_broker.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-09-05T00:00:00+00:00"
            for operation in payloads:
                options = {}
                if operation == "git-receive-pack":
                    options["ref"] = "refs/heads/feat/demo"
                if operation.startswith("pr-"):
                    options["pr_number"] = 7
                if operation == "issue-view":
                    options["issue_number"] = 8
                session.audit(operation=operation, status="ok", bytes_transferred=5, **options)
            session.audit(operation="pr-view", status="denied", pr_number=7)
            session.audit(operation="issue-view", status="error", stage="issue-request", issue_number=8)
        audit = session.audit_file.read_bytes().hex()
    return {"requests": requests, "responses": responses,
            "chunks": stream.getvalue().hex(), "transferred": transferred, "audit": audit}


if __name__ == "__main__":
    print(json.dumps(collect_golden(), ensure_ascii=True, sort_keys=True, indent=2))
```

- [ ] **Step 3: 基準 commit の export で support を実行し fixture を作る。** この command は repository の現在の encoder を import しない。

```python
import os
from pathlib import Path
import subprocess
import sys
import tempfile

base = "a69bb780dc61f3f0f50c92f668f8686837280f12"
support = Path("tests/container/broker_github_golden_support.py").resolve()
with tempfile.TemporaryDirectory(prefix="github-golden-baseline-") as temporary:
    archive = subprocess.check_output(["git", "archive", base])
    subprocess.run(["tar", "-x", "-C", temporary], input=archive, check=True)
    body = subprocess.check_output(
        [sys.executable, str(support)], cwd=temporary,
        env={**os.environ, "PYTHONPATH": str(Path(temporary) / "src"),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
fixture = Path("tests/fixtures/broker_github_golden.json")
fixture.parent.mkdir(parents=True, exist_ok=True)
fixture.write_bytes(body)
```

- [ ] **Step 4: golden test を作り、旧実装で成功することを確認する。** characterization は既存挙動が GREEN であることが基準。後続の新 API の RED と区別する。

```python
import json
from pathlib import Path
import unittest

from tests.container.broker_github_golden_support import collect_golden
from agent_container.github_broker_protocol import decode_request_frame, decode_response_frame


class GitHubGoldenTest(unittest.TestCase):
    def test_wire_and_audit_match_original_commit(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/broker_github_golden.json"
        expected = json.loads(path.read_text())
        self.assertEqual(collect_golden(), expected)
        for operation, encoded in expected["requests"].items():
            raw = bytes.fromhex(encoded)
            request, consumed = decode_request_frame(raw + b"tail")
            self.assertEqual((request.operation, consumed), (operation, len(raw)))
        for status, encoded in expected["responses"].items():
            raw = bytes.fromhex(encoded)
            response, consumed = decode_response_frame(raw + b"tail")
            self.assertEqual((response.status, consumed), (status, len(raw)))
```

compatibility test には次の executable tests を入れる。Python 自体の error message は version 間で異なるので、TypeError の全文を固定せず同一 instance の伝播か型を確認する。

```python
import io
import json
import struct
import sys
import unittest
from unittest import mock

from agent_container import github_broker_protocol as protocol
from agent_container.github_broker_runtime import UploadPackBrokerRuntime


def frame(body):
    return struct.pack(">I", len(body)) + body


class GitHubCompatibilityTest(unittest.TestCase):
    def test_response_legacy_validation(self):
        with self.assertRaisesRegex(ValueError, "^broker request JSON is invalid$"):
            protocol.decode_response_frame(frame(b'{"version":1,"version":1,"status":"ok"}'))
        result, _ = protocol.decode_response_frame(frame(b'{"version":true,"status":"ok"}'))
        self.assertIs(result.version, True)
        with self.assertRaises(TypeError):
            protocol.decode_response_frame(frame(b'{"version":1,"status":[]}'))
        for raw, message in (
            (b"x", "broker response frame is incomplete"),
            (b"\x00\x00\x00\x00", "broker response frame is invalid"),
            (b"\x00\x00\x00\x02x", "broker response frame is invalid"),
            (frame(b'{"version":Infinity,"status":"ok"}'), "broker response schema is invalid"),
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "^" + message + "$"):
                protocol.decode_response_frame(raw)

    def test_request_encoder_and_read_errors_escape(self):
        with self.assertRaises(TypeError):
            protocol.encode_request_frame(protocol.BrokerRequest(1, "A" * 43, "demo", 1, "issue-list", {"x": object()}))
        error = OSError("synthetic-stream-error")
        stream = mock.Mock()
        stream.read.side_effect = error
        with self.assertRaises(OSError) as caught:
            protocol.read_request_frame(stream)
        self.assertIs(caught.exception, error)

    def test_integer_digit_limit_keeps_json_error(self):
        old = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            body = b'{"n":' + b"1" * 641 + b"}"
            with self.assertRaises(ValueError) as reference:
                json.loads(body)
            with self.assertRaises(ValueError) as actual:
                protocol.decode_request_frame(frame(body))
            self.assertEqual(str(actual.exception), str(reference.exception))
        finally:
            sys.set_int_max_str_digits(old)

    def test_short_write_behavior_and_clean_eof_are_preserved(self):
        class ShortWriter(io.BytesIO):
            def write(self, body):
                return super().write(body[:1])
        stream = ShortWriter()
        self.assertEqual(protocol.write_chunk_stream(stream, (b"ab",)), 2)
        self.assertEqual(stream.getvalue(), bytes.fromhex("006100"))
        self.assertEqual(list(protocol.iter_chunk_stream(io.BytesIO(), maximum_total=0, allow_initial_eof=True)), [])
        with self.assertRaisesRegex(ValueError, "broker stream is incomplete"):
            list(protocol.iter_chunk_stream(io.BytesIO(b"\x00"), maximum_total=0, allow_initial_eof=True))

    def test_start_preserves_original_error_and_closes_session(self):
        session = mock.Mock()
        error = OSError("synthetic-start-error")
        session.open_listener.side_effect = error
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        with self.assertRaises(OSError) as caught:
            runtime.__enter__()
        self.assertIs(caught.exception, error)
        session.close.assert_called_once_with()

    def test_stop_order_and_cleanup_error_are_preserved(self):
        events = []
        session = mock.Mock()
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        runtime._thread = mock.Mock()
        runtime._thread.is_alive.return_value = False
        def close_listener():
            self.assertTrue(runtime._stop.is_set())
            events.append("listener")
        session._listener.close.side_effect = close_listener
        runtime._thread.join.side_effect = lambda **kw: events.append(("join", kw))
        error = ValueError("synthetic-cleanup-error")
        def close_session():
            events.append("session")
            raise error
        session.close.side_effect = close_session
        with self.assertRaises(ValueError) as caught:
            runtime.__exit__()
        self.assertIs(caught.exception, error)
        self.assertEqual(events, ["listener", ("join", {"timeout": 2}), "session"])

    def test_serve_suppresses_errors_after_stop(self):
        runtime = UploadPackBrokerRuntime(mock.Mock(), mock.Mock())
        listener = mock.Mock()
        def fail():
            runtime._stop.set()
            raise RuntimeError("synthetic-after-stop")
        listener.accept.side_effect = fail
        runtime._serve(listener)
        self.assertIsNone(runtime._error)
```

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_github_golden tests.container.test_github_broker_compatibility -v
```

Expected: 8 tests pass。support は test discovery の `test*` に含めない。golden の基準生成を再実行して byte 一致も確認する。

- [ ] **Step 5: 新規 4 files を commit する。** `test: characterize GitHub broker before kernel migration`。既存 test を変更しない。

## Task 2: frame と chunk を互換 callback 付きで共通化する

**Files:** Modify `broker/frame.py`、`github_broker_protocol.py`; Create `test_broker_github_primitives.py`。

**Interfaces:**
- `decode_frame(schema, data, *, json_decoder: Callable[[bytes], Any] | None = None) -> tuple[dict[str, Any], int]`。
- `write_chunk_stream(stream: BinaryIO, chunks: Iterable[bytes], *, maximum_chunk: int, label: str) -> int`。
- `iter_chunk_stream(*, read_bytes: Callable[[int, bool], bytes], maximum_total: int, maximum_chunk: int, label: str, allow_initial_eof: bool = False) -> Iterable[bytes]`。
- `read_bytes(size, initial_eof)` は呼び出し側の stream を束縛する。OSError/TypeError/clean EOF の policy は callback が所有。maximum_chunk/label は trusted broker constants。

- [ ] **Step 1: 以下の独立境界テストを作り RED を確認する。** 新しい keyword/関数がないため失敗する。Task 1 の test は引き続き GREEN。

```python
import io
import struct
import unittest
from unittest import mock
from agent_container.broker import frame as kernel


class GitHubPrimitiveFrameTest(unittest.TestCase):
    def test_custom_decoder_keeps_error_and_header_gate(self):
        schema = kernel.FrameSchema("demo", "demo stream", frozenset({"x"}), 8, kernel.JsonOptions())
        error = ValueError("original-decoder-error")
        decoder = mock.Mock(side_effect=error)
        with self.assertRaises(ValueError) as caught:
            kernel.decode_frame(schema, b"\x00\x00\x00\x02{}", json_decoder=decoder)
        self.assertIs(caught.exception, error)
        decoder.assert_called_once_with(b"{}")
        decoder.reset_mock()
        with self.assertRaisesRegex(ValueError, "frame size is invalid"):
            kernel.decode_frame(schema, struct.pack(">I", 9), json_decoder=decoder)
        decoder.assert_not_called()
        with self.assertRaisesRegex(ValueError, "schema is invalid"):
            kernel.decode_frame(schema, b"\x00\x00\x00\x02{}", json_decoder=lambda body: {})

    def test_chunk_limit_is_checked_before_body_read(self):
        reader = mock.Mock(return_value=struct.pack(">I", 4))
        with self.assertRaisesRegex(ValueError, "unit stream is too large"):
            list(kernel.iter_chunk_stream(read_bytes=reader, maximum_total=3, maximum_chunk=4, label="unit stream"))
        reader.assert_called_once_with(4, False)

    def test_chunk_limit_boundary_and_late_eof(self):
        reader = mock.Mock(side_effect=[struct.pack(">I", 4), b"abcd", b"\x00" * 4])
        self.assertEqual(list(kernel.iter_chunk_stream(read_bytes=reader, maximum_total=4, maximum_chunk=4, label="unit stream", allow_initial_eof=True)), [b"abcd"])
        self.assertEqual(reader.call_args_list, [mock.call(4, True), mock.call(4, False), mock.call(4, False)])
        reader = mock.Mock(return_value=struct.pack(">I", 5))
        with self.assertRaisesRegex(ValueError, "chunk is invalid"):
            list(kernel.iter_chunk_stream(read_bytes=reader, maximum_total=6, maximum_chunk=4, label="unit stream"))
        reader.assert_called_once()

    def test_chunk_writer_keeps_framing_and_rejects_empty_chunk(self):
        stream = io.BytesIO()
        self.assertEqual(kernel.write_chunk_stream(stream, [b"ab"], maximum_chunk=2, label="unit stream"), 2)
        self.assertEqual(stream.getvalue(), b"\x00\x00\x00\x02ab\x00\x00\x00\x00")
        with self.assertRaisesRegex(ValueError, "chunk is invalid"):
            kernel.write_chunk_stream(io.BytesIO(), [b""], maximum_chunk=2, label="unit stream")
```

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_github_primitives -v`。

- [ ] **Step 2: decode_frame に callback を追加する。** imports に `Callable`/`Iterable`。既存 header/size/consumed と schema 検査の間で、callback がある場合だけ body を渡す。callback を既定 decoder の例外変換 try に入れない。

```python
# decode_frame signature に *, json_decoder: Callable[[bytes], Any] | None = None
# を追加し、現在の JSON try/except 部分のみ次に置き換える。
if json_decoder is not None:
    decoded = json_decoder(data[HEADER_BYTES:consumed])
else:
    try:
        text = data[HEADER_BYTES:consumed].decode(schema.json.encoding)
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"{schema.frame_prefix} JSON is invalid") from None
```

GitHub に `_REQUEST_SCHEMA` を作り、元の JSON 部分を `_decode_request_json` に移す。`_reject_constant` と `_object_without_duplicates` は残す（response も後者に依存する）。

```python
_REQUEST_SCHEMA = FrameSchema(
    label="broker request", stream_label="broker stream", fields=_REQUEST_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=JsonOptions(ensure_ascii=False, allow_nan=False, separators=(",", ":"), encoding="utf-8"),
)


def _decode_request_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant,
                          object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("broker request JSON is invalid") from None


# decode_request_frame の前半（version = decoded["version"] より前）を置換:
decoded, consumed = decode_frame(_REQUEST_SCHEMA, data, json_decoder=_decode_request_json)
```

typed validation、返却 dataclass、consumed はそのまま。callback は基準の ValueError と traceback context の抑制有無も保つ。

- [ ] **Step 3: chunk primitives を追加する。** short-write retry は入れない。`write_all`/`read_exact` の既存契約は変更しない。

```python
def write_chunk_stream(
    stream: BinaryIO, chunks: Iterable[bytes], *, maximum_chunk: int, label: str
) -> int:
    transferred = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > maximum_chunk:
            raise ValueError(f"{label} chunk is invalid")
        stream.write(struct.pack(">I", len(chunk)))
        stream.write(chunk)
        transferred += len(chunk)
    stream.write(b"\x00\x00\x00\x00")
    stream.flush()
    return transferred


def iter_chunk_stream(
    *, read_bytes: Callable[[int, bool], bytes], maximum_total: int,
    maximum_chunk: int, label: str, allow_initial_eof: bool = False,
) -> Iterable[bytes]:
    if maximum_total < 0:
        raise ValueError(f"{label} limit is invalid")
    transferred = 0
    while True:
        header = read_bytes(HEADER_BYTES, allow_initial_eof and transferred == 0)
        if header == b"":
            return
        length = struct.unpack(">I", header)[0]
        if length == 0:
            return
        if length > maximum_chunk:
            raise ValueError(f"{label} chunk is invalid")
        transferred += length
        if transferred > maximum_total:
            raise ValueError(f"{label} is too large")
        yield read_bytes(length, False)
```

GitHub の wrapper は既存 signature を保持する。kernel imports は別名にする。

```python
from agent_container.broker.frame import iter_chunk_stream as kernel_iter_chunks
from agent_container.broker.frame import write_chunk_stream as kernel_write_chunks


def write_chunk_stream(stream: BinaryIO, chunks: Iterable[bytes]) -> int:
    return kernel_write_chunks(stream, chunks, maximum_chunk=MAX_STREAM_CHUNK_BYTES, label="broker stream")


def iter_chunk_stream(stream: BinaryIO, *, maximum_total: int, allow_initial_eof: bool = False) -> Iterable[bytes]:
    yield from kernel_iter_chunks(
        read_bytes=lambda size, initial: _read_exact(stream, size, initial_eof=initial),
        maximum_total=maximum_total, maximum_chunk=MAX_STREAM_CHUNK_BYTES,
        label="broker stream", allow_initial_eof=allow_initial_eof,
    )
```

- [ ] **Step 4: 新 API tests、Task 1、既存 frame/protocol/transport を実行する。**

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_github_primitives tests.container.test_broker_github_golden tests.container.test_github_broker_compatibility tests.container.test_broker_frame tests.container.test_github_broker_protocol tests.container.test_github_broker_transport -v
```

- [ ] **Step 5: 関連 3 files のみ commit。** `refactor: share GitHub frame and chunk primitives`。

## Task 3: accept iteration を共通化し lifecycle は保存する

**Files:** Modify `broker/runtime.py`、`github_broker_runtime.py`、新規 primitives test。

**Interfaces:** `accept_clients(listener: Any, *, stop_event: threading.Event) -> Iterator[Any]`。listener timeout の設定、client close、stream、thread、error 保存は consumer が所有する。

- [ ] **Step 1: 以下を primitives test に追加して RED を確認する。**

```python
import threading
from agent_container.broker import runtime as runtime_kernel


class AcceptClientsTest(unittest.TestCase):
    def test_timeout_retries_and_consumer_owns_client(self):
        stop = threading.Event()
        client = mock.Mock()
        listener = mock.Mock()
        listener.accept.side_effect = [TimeoutError(), (client, None)]
        clients = runtime_kernel.accept_clients(listener, stop_event=stop)
        self.assertIs(next(clients), client)
        stop.set()
        self.assertEqual(list(clients), [])
        self.assertEqual(listener.accept.call_count, 2)
        client.close.assert_not_called()

    def test_live_error_escapes_but_stopped_error_ends_iteration(self):
        stop = threading.Event()
        listener = mock.Mock()
        error = OSError("synthetic-accept")
        listener.accept.side_effect = error
        with self.assertRaises(OSError) as caught:
            list(runtime_kernel.accept_clients(listener, stop_event=stop))
        self.assertIs(caught.exception, error)
        def fail_after_stop():
            stop.set()
            raise error
        listener.accept.side_effect = fail_after_stop
        self.assertEqual(list(runtime_kernel.accept_clients(listener, stop_event=stop)), [])
```

Run primitives test。Expected: missing `accept_clients`。

- [ ] **Step 2: helper を追加する。** kernel の既存 loop にあった OSError/stop の扱いを移す。

```python
from typing import Iterator


def accept_clients(listener: Any, *, stop_event: threading.Event) -> Iterator[Any]:
    while not stop_event.is_set():
        try:
            client, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            if stop_event.is_set():
                return
            raise
        yield client
```

kernel `_serve` の readiness loop は保存し、その後の accept while/try/except のみ次で置換する。既存の outer except と concurrency body は保存。

```python
for client in accept_clients(listener, stop_event=self.stop_event):
    if self.concurrency == "thread":
        self._start_worker(client)
    else:
        with client:
            self._handle_client(client)
```

GitHub `_serve` も accept while/try/except のみ次で置換し、`with client` 以下と outer except を元のまま残す。

```python
for client in accept_clients(listener, stop_event=self._stop):
    with client:
        stream = client.makefile("rwb", buffering=0)
        try:
            handle_broker_connection(
                self.session, stream, self.transport, self.receive_transport,
                self.pr_transport, self.issue_transport,
            )
        finally:
            stream.close()
```

`__enter__`/`__exit__` を `SocketBrokerRuntime.start/stop` に置換しない。停止後の OSError は iterator が終了するだけなので GitHub outer except が以前握り潰した結果を保存する。他の BaseException の判定は元の outer except に残る。

- [ ] **Step 3: primitives/compatibility と既存 kernel/handover/egress/runtime suites を実行。**

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_github_primitives tests.container.test_github_broker_compatibility tests.container.test_broker_runtime tests.container.test_handover_broker_runtime tests.container.test_egress_broker_runtime tests.container.test_egress_broker_runtime_surface tests.container.test_github_broker_runtime -v
```

- [ ] **Step 4: 3 files を commit。** `refactor: share broker accept iteration`。

## Task 4: session の一致する資源確保と audit write を共通化する

**Files:** Modify `broker/audit.py`、`github_broker.py`、新規 primitives test。

**Interfaces:** 既存 `allocate_run_dir(project_root, *, label, attempts=8) -> tuple[str, Path]`。新規 `append_text_record(stream: TextIO, record: dict[str, object]) -> None`。opener/close は caller が所有する。helper は json.dump→newline→flush→fileno→fsync を保持する。

- [ ] **Step 1: 次の test を追加し新 helper がない RED を確認。**

```python
from agent_container.broker import audit as audit_kernel


class TextAuditTest(unittest.TestCase):
    def test_text_writer_preserves_chunks_flush_and_fsync(self):
        events = []
        class Stream:
            def write(self, value):
                events.append(("write", value))
            def flush(self):
                events.append(("flush",))
            def fileno(self):
                events.append(("fileno",))
                return 91
        with mock.patch.object(audit_kernel.os, "fsync", side_effect=lambda fd: events.append(("fsync", fd))):
            audit_kernel.append_text_record(Stream(), {"x": "日本語"})
        self.assertEqual("".join(event[1] for event in events if event[0] == "write"), '{"x":"\\u65e5\\u672c\\u8a9e"}\n')
        self.assertEqual(events[-3:], [("flush",), ("fileno",), ("fsync", 91)])

    def test_flush_error_prevents_fsync_and_escapes(self):
        stream = mock.Mock()
        error = OSError("synthetic-audit-flush")
        stream.flush.side_effect = error
        with mock.patch.object(audit_kernel.os, "fsync") as fsync:
            with self.assertRaises(OSError) as caught:
                audit_kernel.append_text_record(stream, {"x": 1})
        self.assertIs(caught.exception, error)
        fsync.assert_not_called()
```

- [ ] **Step 2: helper を追加し GitHub の writer を置換する。** `AuditLog` は TextIO を使っていないので変更しない。ここは保証の弱い opener を kernel の既定として追加するものではない。

```python
from typing import TextIO


def append_text_record(stream: TextIO, record: dict[str, object]) -> None:
    json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
```

GitHub `audit` の末尾のみ置換する。

```python
with _open_audit_file(self.audit_file) as stream:
    append_text_record(stream, record)
```

`json` import は GitHub で他の用途がなくなれば削除する。record の生成・検証・順序と opener は変更しない。

- [ ] **Step 3: run directory 確保の loop のみ委譲する。**

```python
run_id, run_dir = allocate_run_dir(project_root, label="broker")
```

直後の `capability = secrets.token_urlsafe(32)` からは元のまま。`generate_capability` の RuntimeError を catch して cleanup する置換は、random source 自体が RuntimeError を投げた時の旧挙動を変え得るため採らない。file creation、bind、session.close は既存コードを残す。

- [ ] **Step 4: Task 1、primitives、既存 audit/session suites を実行する。**

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_github_primitives tests.container.test_broker_github_golden tests.container.test_github_broker_compatibility tests.container.test_broker_audit tests.container.test_broker_runtime tests.container.test_github_broker -v
```

- [ ] **Step 5: 3 files を commit。** `refactor: share GitHub run allocation and audit writes`。

## Task 5: 全体検証と仕様・PR の整合

**Files:** `CHANGELOG.md`、spec、roadmap、計画。既存 tests は変更しない。

**Interfaces:** consumes Tasks 1–4。produces 検証記録と reviewable PR。6-4 完了はこの限定範囲の完了であり、Phase 6 全体の完了ではない。

- [ ] **Step 1: source/test 境界を検証する。**

```sh
git diff --check
git diff --stat a69bb780dc61f3f0f50c92f668f8686837280f12 -- src tests
git diff --exit-code a69bb780dc61f3f0f50c92f668f8686837280f12 -- src/agent_container/github_broker_transport.py src/agent_container/egress_adapter.py src/agent_container/broker/capability.py src/agent_container/broker/readiness.py src/agent_container/podman.py src/agent_container/agentctl.py src/agent_container/state.py
```

変更前から存在した test files の不変性を machine-check する。

```python
import subprocess
base = "a69bb780dc61f3f0f50c92f668f8686837280f12"
old = set(subprocess.check_output(["git", "ls-tree", "-r", "--name-only", base, "tests/"]).decode().splitlines())
changed = set(subprocess.check_output(["git", "diff", "--name-only", base, "--", "tests/"]).decode().splitlines())
assert not old & changed, sorted(old & changed)
```

- [ ] **Step 2: 必須 local validation。** 最終 tree に対して一度実行し、成功後は変更や懸念がなければ繰り返さない。

```sh
bin/lint
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex
```

socket は CI と同じ 3 suites と ResourceWarning gate を使う。

```sh
socket_log=$(mktemp)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 python3 -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket -v 2>"$socket_log"
socket_status=$?
cat "$socket_log"
if rg -F ResourceWarning "$socket_log"; then
    socket_status=1
fi
rm -f "$socket_log"
test "$socket_status" -eq 0
```

上の socket block は `set -e` なしの shell で実行して exit status を回収する。Podman は CI の既存 14 tests/no skips gate。実 host smoke は `not run — 6-6`。Podman のない local 環境は `not run — podman unavailable` と CI 成否を分けて記録する。

既知の環境依存: handover wrapper test は session broker env の継承で失敗し得る。単独 Podman test は tempfile 初回探索と os.write mock が干渉する。Family Podman の fault injection race は既存で、再実行成功を修正完了とは扱わない。詳細は investigation。これらを修正するために既存 test/production を本 PR で編集しない。

- [ ] **Step 3: docs を実装の事実に合わせる。** CHANGELOG の Unreleased に次を入れ、Validation には実測件数・commit・not run の理由を書く。

> GitHub broker の frame/chunk、accept iteration、run directory 確保、audit write を共通 broker kernel へ移動。既存の JSON 例外、lifecycle、cleanup、capability 検証と audit opener は互換性のため維持し、完全統一を stage 2 に残す。

spec と roadmap は本計画作成時点で承認範囲へ更新済み。実装が計画と違った場合のみ更新し、6-4 や Phase 6 を実装前に完了表記しない。

- [ ] **Step 4: 全差分を review する。** 特に callback exception identity、generator の遅延評価、OSError after stop、client/stream ownership、audit open/write/fsync/close の順序、short-write の意図しない修正、残した capability 検証の権限拡大がないこと。review 所見は file/再現/仕様根拠を付ける。
- [ ] **Step 5: docs と検証結果を commit し PR を作る。** タイトル: `refactor: share compatible GitHub broker primitives (Phase 6-4)`。本文は抽出/残置を列挙し、既存 tests 不変の証拠、golden 基準 commit、実行結果、既存 Family race を区別する。必須 CI 成功前に merge しない。外部へのコメント/通知は既存の依頼・承認範囲を確認する。

## Self-review（計画作成時）

- 承認済み scope を Tasks 1–5 と対応付けた。frame/chunk は Task 2、accept は Task 3、allocation/audit は Task 4、既存契約の証明は Tasks 1/5。
- kernel の既存 consumers は default decoder、既存 AuditLog、read_exact/write_all を維持する。Task 3 の accept 部分のみ同じ loop を移動する。
- JSON decoder の callback と read_bytes の署名、wrapper の import 別名を統一した。kernel は GitHub を import しない。
- private surface は tests 全体（integration を含む）を検索し記録した。
- golden は旧 commit で生成する executable 手順を含む。fixture は後続 task の新 encoder から更新しない。
- 全面 lifecycle/capability/audit-open 統一、既存 bug 修正、実 host smoke を未実施のまま完了扱いしない。

## 計画自体の検証（2026-09-05）

- 18 個の Python block を AST parse し構文確認した。
- 基準 commit の使い捨て export で golden/compatibility test 8 件が成功。golden 生成を 2 回行い byte 一致。
- 新 API test 8 件は基準 export で API 不在による RED を確認し、計画記載の kernel 実装例だけを一時 export に加えて 8 件の GREEN を確認した。これは本番側の wrapper 乗せ替えや実装完了の検証ではない。
- workspace の既存 `tests.container.test_docs` は 67 件成功。追跡差分と新規 Markdown の whitespace/参照先を確認。
- source/tests の workspace 差分なし。全 container/Codex/lint/CI は `not run — 今回は計画・仕様文書のみの変更`。新実装に対する必須検証は Task 5 で実行する。
- 実 Podman/実 host smoke は `not run — 実装未着手、実 host smoke は 6-6`。
