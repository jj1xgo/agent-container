# Phase 6-1: broker kernel `frame` / `capability` + handover 乗せ替え Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `src/agent_container/broker/` に `frame.py`（length-prefixed JSON frame codec とstream helper）と `capability.py`（container側の exact path / capability file / socket 検証と接続）を抽出し、handover broker の protocol module と container側 client をその上に乗せ替える。wire byte、audit、既存 handover test は一切変えない。

**Architecture:** kernel は broker 固有 module を import しない（依存は `handover_* → broker/` の一方向）。`handover_broker_protocol.py` は `HandoverRequest`/`HandoverResponse`、定数、field 型検証を残し、frame の encode/decode/read を `broker.frame` に委ねる。`handover_broker_client.py` は `_validate_exact_path`/`read_handover_capability`/`validate_handover_socket` を **同名のまま** 薄い wrapper として残し（既存 test が `mock.patch` でこれらの名前を差し替えるため）、実体を `broker.capability` に置く。振る舞い保存は (a) 既存 test 不変、(b) kernel化前 encoder（commit `b1198d1`）で生成した golden byte fixture、で証明する。

**Tech Stack:** Python 3.11（`ruff.toml` target-version）、標準library のみ（`json`, `struct`, `os`, `socket`, `dataclasses`）、`unittest`（`PYTHONPATH=src python3 -m unittest ...`）、ruff 0.16.4（`bin/lint`）。

**Spec:** `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`（Phase 6 / stage 1）。本 plan は spec の「乗せ替えの順序とPR分割」の **6-1** のみを実装する。6-2 以降は本 PR の merge 後に別 plan を書く。

## Context

roadmap は Phase 6 を「共通 broker kernel」と定義した（PR #88）。4 broker が個別に持つ frame codec / capability 読み取りの重複を、既存 test を変えずに kernel へ寄せる最初の一手が 6-1 である。handover を最初にする理由: operation が `create` 1 つ、守るべき追加保証がなく、audit が最も単純。

**plan 作成中に判明し、spec を訂正する事実（Task 7 で反映）:**
- spec の「egress は `json.dumps` 全て既定」は誤り。実際は request `allow_nan=False, separators=(",", ":"), sort_keys=True` を ascii、response `separators=(",", ":"), sort_keys=True` を ascii。github は request `ensure_ascii=False, allow_nan=False, separators=(",", ":")` を utf-8、response `separators=(",", ":"), sort_keys=True` を ascii。**request と response で options が違う broker がある**ので、`FrameSchema` は request/response それぞれに宣言する。
- spec の「`test_image.py` の変更は不要な見込み」は誤り。`test_effective_image_tree_imports_host_entrypoints_without_host_modules`（`tests/container/test_image.py:555`）は `glob("*.py")` で **top-level の .py だけ** を仮 image tree へ copy して `agent_container.podman` を import する。`podman.py → handover_broker_runtime → handover_broker → handover_broker_protocol → broker.frame` の import 連鎖により、subpackage `broker/` が copy されないと import が失敗する。Task 2 で copy を subdirectory 対応にする（broker test ではなく image contract test の infra 修正で、spec の「既存 broker test 不変」規則の対象外）。

## Global Constraints

- `PROTOCOL_VERSION` は `1` のまま。wire byte 列を変えない（golden fixture で固定）。
- kernel（`src/agent_container/broker/`）は `agent_container.handover_*` / `egress_*` / `github_*` / `family_*` / `state` を import しない。標準 library のみ。
- 次の既存 test file は **1 byte も変更しない**: `tests/container/test_handover_broker_protocol.py`、`tests/container/test_handover_broker_client.py`、`tests/container/test_handover_broker_runtime.py`、`tests/container/test_handover_broker_transport.py`、`tests/container/test_handover_broker.py`、`tests/integration/test_handover_broker_socket.py`。変更が必要になったら止めて報告する。
- handover の ValueError message 文字列を保存する（`"handover broker request frame is incomplete"` 等）。message は test で検証されていないが、stage 1 の「振る舞い保存」の一部として維持する。
- 作業は `.worktrees/feat-broker-kernel-frame` の worktree、branch `feat/broker-kernel-frame`。worktree 作成後に `chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json`（既知の環境要因: 新 worktree で 0664 になり `test_claude_policy` が落ちる）。
- test 実行: `cd <worktree> && PYTHONPATH=src python3 -m unittest <module> -v`。lint: `bin/lint`。
- commit message 末尾: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` と `Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm`。
- 既存 broker の bug を見つけても直さない（Issue 化して別 PR）。

---

## File Structure

| path | 役割 |
| --- | --- |
| `docs/superpowers/plans/2026-09-04-broker-kernel-6-1-frame-capability.md` | **Create.** 本 plan の写し（repo 規約: plan は `docs/superpowers/plans/` に置く） |
| `src/agent_container/broker/__init__.py` | **Create.** docstring のみ |
| `src/agent_container/broker/frame.py` | **Create.** `JsonOptions`、`FrameSchema`、`encode_frame`、`decode_frame`、`read_exact`、`read_frame`、`write_all` |
| `src/agent_container/broker/capability.py` | **Create.** `CAPABILITY_PATTERN`、`validate_exact_path`、`read_capability`、`validate_socket`、`connect_unix` |
| `src/agent_container/handover_broker_protocol.py` | **Modify.** `_reject_constant`/`_object_without_duplicates`/`_decode_json`/`_read_exact`/`_HEADER_BYTES` を削除し、`broker.frame` に委ねる。公開名は全て維持 |
| `src/agent_container/handover_broker_client.py` | **Modify.** `_validate_exact_path`/`read_handover_capability`/`validate_handover_socket` を `broker.capability` への wrapper にし、`create()` の接続と送信を `connect_unix`/`write_all` に置き換える。公開名・`os` import・`_CAPABILITY`・`_SOCKET_TIMEOUT_SECONDS`・`_self_check` は維持 |
| `tests/container/test_broker_frame_golden.py` | **Create.** commit `b1198d1` の encoder から生成した静的 byte 列で handover frame を固定 |
| `tests/container/test_broker_frame.py` | **Create.** kernel `frame` の unit test |
| `tests/container/test_broker_capability.py` | **Create.** kernel `capability` の unit test |
| `tests/container/test_image.py:555-558` | **Modify.** 仮 image tree の copy を subdirectory 対応にする |
| `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` | **Modify.** 上記「訂正する事実」を反映 |
| `CHANGELOG.md` | **Modify.** Unreleased / Added に 1 行 |

## Interfaces（全 task 共通の contract）

`src/agent_container/broker/frame.py`:

```python
HEADER_BYTES = 4

@dataclass(frozen=True)
class JsonOptions:
    ensure_ascii: bool = True
    allow_nan: bool = True
    sort_keys: bool = False
    separators: tuple[str, str] | None = None
    encoding: str = "utf-8"          # encode 時の str.encode / decode 時の bytes.decode に使う

@dataclass(frozen=True)
class FrameSchema:
    label: str                       # 例 "handover broker request"（message prefix）
    stream_label: str                # 例 "handover broker stream"
    fields: frozenset[str]           # decode 後の key 集合と完全一致を要求
    max_bytes: int                   # body（header を除く）の上限
    json: JsonOptions

def encode_frame(schema: FrameSchema, values: dict[str, Any]) -> bytes
def decode_frame(schema: FrameSchema, data: bytes) -> tuple[dict[str, Any], int]
def read_exact(stream: BinaryIO, size: int, *, label: str) -> bytes
def read_frame(schema: FrameSchema, stream: BinaryIO) -> dict[str, Any]
def write_all(stream: BinaryIO, frame: bytes, *, label: str) -> None
```

error message（`ValueError`）の規則。`{label}` は `schema.label`、`{stream}` は `schema.stream_label` または `read_exact`/`write_all` の `label` 引数:

| 条件 | message |
| --- | --- |
| `encode_frame`: `json.dumps` が `TypeError`/`UnicodeEncodeError`/`ValueError` | `{label} is invalid` |
| `encode_frame`: body が空、または `len(body) > max_bytes` | `{label} is too large` |
| `decode_frame`: `data` が bytes でない、または `len(data) < 4` | `{label} frame is incomplete` |
| `decode_frame`: length が 0 または `> max_bytes` | `{label} frame size is invalid` |
| `decode_frame`: `len(data) < 4 + length` | `{label} frame is incomplete` |
| `decode_frame`: decode/`json.loads` 失敗、重複 key、`NaN`/`Infinity` | `{label} JSON is invalid` |
| `decode_frame`: JSON が object でない、または key 集合 ≠ `fields` | `{label} schema is invalid` |
| `read_exact`: `stream.read` が `OSError`/`TypeError`/`ValueError` | `{stream} is invalid` |
| `read_exact`: chunk が bytes でない、空、または要求より長い | `{stream} is incomplete` |
| `read_frame`: header の length が 0 または `> max_bytes` | `{label} frame size is invalid` |
| `read_frame`: `decode_frame` の consumed ≠ `4 + len(body)` | `{label} frame is invalid` |
| `write_all`: `stream.write` が bool、int でない、`<= 0`、残り長より大 | `{label} write failed` |

`src/agent_container/broker/capability.py`:

```python
CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")

def validate_exact_path(path: Path, *, label: str) -> Path
    # 絶対 path、resolve(strict=True) が同一、でなければ ValueError(f"{label} is invalid")
def read_capability(path: Path, *, label: str) -> str
    # handover の read_handover_capability と同一 semantics（O_RDONLY|O_NOFOLLOW|O_NONBLOCK、
    # fstat: 通常 file・mode 0o600・uid 一致・size 44、45 byte まで読む、resolve と lstat の
    # dev/ino が fstat と一致、ascii、43 文字 + "\n"）。失敗は全て ValueError(f"{label} is invalid")
def validate_socket(path: Path, *, label: str) -> Path
    # path.stat() が S_ISSOCK・mode 0o600・uid 一致、でなければ ValueError(f"{label} is invalid")
    # ★ path の resolve はしない（呼び出し側が validate_exact_path を先に呼ぶ）
def connect_unix(path: Path, *, timeout: float, socket_factory: Callable[..., Any] = socket.socket) -> Any
    # sock = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM); settimeout(timeout); connect(str(path))
    # connect が失敗したら sock.close() してから re-raise
```

---

### Task 1: worktree 準備と golden fixture（kernel 化前の encoder で固定）

**Files:**
- Create: `docs/superpowers/plans/2026-09-04-broker-kernel-6-1-frame-capability.md`
- Create: `tests/container/test_broker_frame_golden.py`

**Interfaces:**
- Consumes: 現行の `agent_container.handover_broker_protocol`（commit `b1198d1`）
- Produces: golden byte 列 3 件（後続 task の回帰網）

- [ ] **Step 1: worktree を作る**

```bash
cd /home/tsu/Projects/agent-container
git status --short   # 空であること
git worktree add .worktrees/feat-broker-kernel-frame -b feat/broker-kernel-frame main
cd .worktrees/feat-broker-kernel-frame
chmod 644 profiles/claude/statusline.sh profiles/claude/CLAUDE.md profiles/claude/managed-settings.json profiles/claude/managed-mcp.json
git rev-parse --short HEAD   # b1198d1 であること（golden の出自）
```

- [ ] **Step 2: plan を repo に写す**

`/home/tsu/.claude/plans/jaunty-skipping-wall.md` の内容を `docs/superpowers/plans/2026-09-04-broker-kernel-6-1-frame-capability.md` へそのまま保存する。

- [ ] **Step 3: golden test を書く（現行 encoder で PASS することを確認する test）**

`tests/container/test_broker_frame_golden.py`:

```python
"""Golden frames captured from the pre-kernel handover encoder.

Generated at commit b1198d1 with agent_container.handover_broker_protocol
before any broker kernel existed. Never regenerate these bytes from the
kernel's own output; a mismatch here means the wire format changed.
"""

from io import BytesIO
import unittest

from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import HandoverResponse
from agent_container.handover_broker_protocol import decode_request_frame
from agent_container.handover_broker_protocol import decode_response_frame
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import encode_response_frame
from agent_container.handover_broker_protocol import read_request_frame
from agent_container.handover_broker_protocol import read_response_frame


GOLDEN_BODY = "## 作業の目的\n目的\n\n## 現在地\n現在地\n"
GOLDEN_REQUEST = HandoverRequest(
    1, "A" * 43, "agent-container", "create", "Golden タイトル", GOLDEN_BODY
)
GOLDEN_REQUEST_BYTES = (
    b'\x00\x00\x00\xdb{"version":1,"capability":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
    b'"project_id":"agent-container","operation":"create",'
    b'"title":"Golden \xe3\x82\xbf\xe3\x82\xa4\xe3\x83\x88\xe3\x83\xab",'
    b'"body":"## \xe4\xbd\x9c\xe6\xa5\xad\xe3\x81\xae\xe7\x9b\xae\xe7\x9a\x84\\n'
    b'\xe7\x9b\xae\xe7\x9a\x84\\n\\n## \xe7\x8f\xbe\xe5\x9c\xa8\xe5\x9c\xb0\\n'
    b'\xe7\x8f\xbe\xe5\x9c\xa8\xe5\x9c\xb0\\n"}'
)
GOLDEN_RESPONSE_OK = HandoverResponse(
    1, "ok", "/handovers/agent-container/2026-09-04_000000_deadbeef.md", ""
)
GOLDEN_RESPONSE_OK_BYTES = (
    b'\x00\x00\x00g{"version":1,"status":"ok",'
    b'"path":"/handovers/agent-container/2026-09-04_000000_deadbeef.md","code":""}'
)
GOLDEN_RESPONSE_DENIED = HandoverResponse(1, "denied", "", "authentication")
GOLDEN_RESPONSE_DENIED_BYTES = (
    b'\x00\x00\x00A{"version":1,"status":"denied","path":"","code":"authentication"}'
)


class HandoverGoldenFrameTest(unittest.TestCase):
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

    def test_response_bytes_decode_to_the_same_values(self) -> None:
        self.assertEqual(
            decode_response_frame(GOLDEN_RESPONSE_OK_BYTES),
            (GOLDEN_RESPONSE_OK, len(GOLDEN_RESPONSE_OK_BYTES)),
        )
        self.assertEqual(
            read_response_frame(BytesIO(GOLDEN_RESPONSE_DENIED_BYTES)),
            GOLDEN_RESPONSE_DENIED,
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: 現行 encoder で PASS することを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame_golden -v`
Expected: 4 tests, OK。（FAIL なら byte 列の転記ミス。上の値は `b1198d1` で実測したもの）

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-09-04-broker-kernel-6-1-frame-capability.md tests/container/test_broker_frame_golden.py
git commit -m "test: pin handover wire bytes before extracting the broker kernel

Golden frames generated by the pre-kernel encoder at b1198d1. The
kernel extraction that follows must reproduce these bytes exactly.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 2: image contract test を subpackage 対応にする

**Files:**
- Modify: `tests/container/test_image.py:555-558`

**Interfaces:**
- Produces: `src/agent_container/<subdir>/*.py` が仮 image tree へ copy される

- [ ] **Step 1: copy loop を rglob + directory 保持に変える**

`tests/container/test_image.py` の `test_effective_image_tree_imports_host_entrypoints_without_host_modules` 内、

```python
            for source in (ROOT / "src/agent_container").glob("*.py"):
                relative = source.relative_to(ROOT).as_posix()
                if containerignore_includes(relative, patterns):
                    shutil.copy2(source, target / source.name)
```

を次に置き換える:

```python
            for source in sorted((ROOT / "src/agent_container").rglob("*.py")):
                relative = source.relative_to(ROOT).as_posix()
                if containerignore_includes(relative, patterns):
                    destination = target / source.relative_to(ROOT / "src/agent_container")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
```

- [ ] **Step 2: 現在の tree（subpackage なし）で同じ結果になることを確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image -v`
Expected: 17 tests, OK（`__pycache__` は `*.py` に一致しないので copy されない）

- [ ] **Step 3: Commit**

```bash
git add tests/container/test_image.py
git commit -m "test: copy source subpackages into the image import probe

The probe rebuilt the image source tree from top-level modules only,
so a subpackage under src/agent_container could never be imported.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 3: `broker/frame.py`（TDD）

**Files:**
- Create: `src/agent_container/broker/__init__.py`
- Create: `src/agent_container/broker/frame.py`
- Test: `tests/container/test_broker_frame.py`

**Interfaces:**
- Produces: 「Interfaces」節の `JsonOptions`、`FrameSchema`、`encode_frame`、`decode_frame`、`read_exact`、`read_frame`、`write_all`、`HEADER_BYTES`

- [ ] **Step 1: failing test を書く**

`tests/container/test_broker_frame.py`:

```python
from io import BytesIO
import struct
import unittest
from unittest import mock

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import HEADER_BYTES
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import read_exact
from agent_container.broker.frame import read_frame
from agent_container.broker.frame import write_all


COMPACT = JsonOptions(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
SCHEMA = FrameSchema(
    label="test request",
    stream_label="test stream",
    fields=frozenset({"version", "name"}),
    max_bytes=64,
    json=COMPACT,
)
ASCII_SORTED = FrameSchema(
    label="test response",
    stream_label="test stream",
    fields=frozenset({"b", "a"}),
    max_bytes=64,
    json=JsonOptions(separators=(",", ":"), sort_keys=True, encoding="ascii"),
)


def frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


class EncodeFrameTest(unittest.TestCase):
    def test_encodes_header_then_body_with_declared_options(self) -> None:
        self.assertEqual(HEADER_BYTES, 4)
        self.assertEqual(
            encode_frame(SCHEMA, {"version": 1, "name": "名前"}),
            frame('{"version":1,"name":"名前"}'.encode("utf-8")),
        )
        self.assertEqual(
            encode_frame(ASCII_SORTED, {"b": 2, "a": "é"}),
            frame(b'{"a":"\\u00e9","b":2}'),
        )

    def test_rejects_unserializable_nan_and_oversize_values(self) -> None:
        cases = (
            ({"version": object(), "name": "x"}, "test request is invalid"),
            ({"version": float("nan"), "name": "x"}, "test request is invalid"),
            ({"version": 1, "name": "x" * 64}, "test request is too large"),
        )
        for values, message in cases:
            with self.subTest(values=values), self.assertRaises(ValueError) as raised:
                encode_frame(SCHEMA, values)
            self.assertEqual(str(raised.exception), message)

    def test_ascii_encoding_rejects_non_ascii_when_ensure_ascii_is_off(self) -> None:
        schema = FrameSchema(
            label="ascii only",
            stream_label="s",
            fields=frozenset({"a"}),
            max_bytes=64,
            json=JsonOptions(ensure_ascii=False, encoding="ascii"),
        )
        with self.assertRaises(ValueError) as raised:
            encode_frame(schema, {"a": "é"})
        self.assertEqual(str(raised.exception), "ascii only is invalid")


class DecodeFrameTest(unittest.TestCase):
    def test_decodes_exact_fields_and_reports_consumed_length(self) -> None:
        payload = frame(b'{"version":1,"name":"x"}')
        self.assertEqual(
            decode_frame(SCHEMA, payload + b"tail"),
            ({"version": 1, "name": "x"}, len(payload)),
        )

    def test_rejects_short_zero_oversize_and_truncated_frames(self) -> None:
        cases = (
            (b"\x00\x00\x00", "test request frame is incomplete"),
            (b"\x00\x00\x00\x00", "test request frame size is invalid"),
            (struct.pack(">I", 65), "test request frame size is invalid"),
            (b"\x00\x00\x00\x05{}", "test request frame is incomplete"),
        )
        for data, message in cases:
            with self.subTest(data=data), self.assertRaises(ValueError) as raised:
                decode_frame(SCHEMA, data)
            self.assertEqual(str(raised.exception), message)

    def test_rejects_non_bytes_input(self) -> None:
        with self.assertRaises(ValueError) as raised:
            decode_frame(SCHEMA, "text")  # type: ignore[arg-type]
        self.assertEqual(str(raised.exception), "test request frame is incomplete")

    def test_rejects_invalid_json_duplicates_constants_and_bad_encoding(self) -> None:
        cases = (
            frame(b"{"),
            frame(b'{"version":1,"version":1}'),
            frame(b'{"version":NaN,"name":"x"}'),
            frame(b'{"version":Infinity,"name":"x"}'),
            frame(b"\xff"),
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError) as raised:
                decode_frame(SCHEMA, data)
            self.assertEqual(str(raised.exception), "test request JSON is invalid")

    def test_rejects_non_object_missing_and_extra_fields(self) -> None:
        cases = (
            frame(b"[1]"),
            frame(b'"x"'),
            frame(b'{"version":1}'),
            frame(b'{"version":1,"name":"x","extra":1}'),
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError) as raised:
                decode_frame(SCHEMA, data)
            self.assertEqual(str(raised.exception), "test request schema is invalid")

    def test_ascii_schema_rejects_utf8_body(self) -> None:
        with self.assertRaises(ValueError) as raised:
            decode_frame(ASCII_SORTED, frame('{"a":"é","b":2}'.encode("utf-8")))
        self.assertEqual(str(raised.exception), "test response JSON is invalid")


class ReadExactTest(unittest.TestCase):
    def test_reads_until_size_and_only_size(self) -> None:
        stream = BytesIO(b"abcdef")
        self.assertEqual(read_exact(stream, 4, label="test stream"), b"abcd")
        self.assertEqual(stream.read(), b"ef")

    def test_rejects_eof_short_chunk_types_and_overlong_chunks(self) -> None:
        with self.assertRaises(ValueError) as raised:
            read_exact(BytesIO(b"ab"), 4, label="test stream")
        self.assertEqual(str(raised.exception), "test stream is incomplete")
        for chunk in ("ab", None, b"abcde"):
            stream = mock.Mock()
            stream.read.return_value = chunk
            with self.subTest(chunk=chunk), self.assertRaises(ValueError) as raised:
                read_exact(stream, 4, label="test stream")
            self.assertEqual(str(raised.exception), "test stream is incomplete")

    def test_wraps_stream_errors(self) -> None:
        for error in (OSError("io"), TypeError("t"), ValueError("v")):
            stream = mock.Mock()
            stream.read.side_effect = error
            with self.subTest(error=error), self.assertRaises(ValueError) as raised:
                read_exact(stream, 4, label="test stream")
            self.assertEqual(str(raised.exception), "test stream is invalid")


class ReadFrameTest(unittest.TestCase):
    def test_reads_one_frame_and_leaves_the_rest(self) -> None:
        payload = frame(b'{"version":1,"name":"x"}')
        stream = BytesIO(payload + b"following")
        self.assertEqual(read_frame(SCHEMA, stream), {"version": 1, "name": "x"})
        self.assertEqual(stream.read(), b"following")

    def test_rejects_zero_and_oversize_lengths_before_reading_body(self) -> None:
        for header in (b"\x00\x00\x00\x00", struct.pack(">I", 65)):
            stream = BytesIO(header + b"x" * 70)
            with self.subTest(header=header), self.assertRaises(ValueError) as raised:
                read_frame(SCHEMA, stream)
            self.assertEqual(str(raised.exception), "test request frame size is invalid")
            self.assertEqual(stream.tell(), 4)

    def test_rejects_truncated_body(self) -> None:
        with self.assertRaises(ValueError) as raised:
            read_frame(SCHEMA, BytesIO(b"\x00\x00\x00\x05{}"))
        self.assertEqual(str(raised.exception), "test stream is incomplete")


class WriteAllTest(unittest.TestCase):
    def test_retries_partial_writes_until_complete_then_flushes(self) -> None:
        writes: list[bytes] = []
        stream = mock.Mock()

        def partial(body: bytes) -> int:
            writes.append(body[:3])
            return min(3, len(body))

        stream.write.side_effect = partial
        write_all(stream, b"abcdefgh", label="test request")
        self.assertEqual(b"".join(writes), b"abcdefgh")
        stream.flush.assert_called_once_with()

    def test_rejects_invalid_write_progress(self) -> None:
        for progress in (None, True, 0, -1, 9):
            stream = mock.Mock()
            stream.write.return_value = progress
            with self.subTest(progress=progress), self.assertRaises(ValueError) as raised:
                write_all(stream, b"abcdefgh", label="test request")
            self.assertEqual(str(raised.exception), "test request write failed")
            stream.flush.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame -v`
Expected: `ModuleNotFoundError: No module named 'agent_container.broker'`

- [ ] **Step 3: 実装**

`src/agent_container/broker/__init__.py`:

```python
"""Shared kernel for agent-container brokers.

Modules here must not import broker-specific modules
(handover_*, egress_*, github_*, family_*) or agent_container.state.
"""
```

`src/agent_container/broker/frame.py`:

```python
"""Length-prefixed JSON frames shared by every broker protocol."""

from dataclasses import dataclass
import json
import struct
from typing import Any
from typing import BinaryIO


HEADER_BYTES = 4


@dataclass(frozen=True)
class JsonOptions:
    ensure_ascii: bool = True
    allow_nan: bool = True
    sort_keys: bool = False
    separators: tuple[str, str] | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True)
class FrameSchema:
    label: str
    stream_label: str
    fields: frozenset[str]
    max_bytes: int
    json: JsonOptions


def _reject_constant(_: str) -> None:
    raise ValueError("JSON constant is not allowed")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object has a duplicate key")
        result[key] = value
    return result


def encode_frame(schema: FrameSchema, values: dict[str, Any]) -> bytes:
    options = schema.json
    try:
        body = json.dumps(
            values,
            ensure_ascii=options.ensure_ascii,
            allow_nan=options.allow_nan,
            sort_keys=options.sort_keys,
            separators=options.separators,
        ).encode(options.encoding)
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ValueError(f"{schema.label} is invalid") from None
    if not body or len(body) > schema.max_bytes:
        raise ValueError(f"{schema.label} is too large")
    return struct.pack(">I", len(body)) + body


def decode_frame(schema: FrameSchema, data: bytes) -> tuple[dict[str, Any], int]:
    if not isinstance(data, bytes) or len(data) < HEADER_BYTES:
        raise ValueError(f"{schema.label} frame is incomplete")
    length = struct.unpack(">I", data[:HEADER_BYTES])[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.label} frame size is invalid")
    consumed = HEADER_BYTES + length
    if len(data) < consumed:
        raise ValueError(f"{schema.label} frame is incomplete")
    try:
        text = data[HEADER_BYTES:consumed].decode(schema.json.encoding)
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(f"{schema.label} JSON is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != schema.fields:
        raise ValueError(f"{schema.label} schema is invalid")
    return decoded, consumed


def read_exact(stream: BinaryIO, size: int, *, label: str) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise ValueError(f"{label} is invalid") from None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(output):
            raise ValueError(f"{label} is incomplete")
        output.extend(chunk)
    return bytes(output)


def read_frame(schema: FrameSchema, stream: BinaryIO) -> dict[str, Any]:
    header = read_exact(stream, HEADER_BYTES, label=schema.stream_label)
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > schema.max_bytes:
        raise ValueError(f"{schema.label} frame size is invalid")
    body = read_exact(stream, length, label=schema.stream_label)
    decoded, consumed = decode_frame(schema, header + body)
    if consumed != len(header) + len(body):
        raise ValueError(f"{schema.label} frame is invalid")
    return decoded


def write_all(stream: BinaryIO, frame: bytes, *, label: str) -> None:
    offset = 0
    while offset < len(frame):
        written = stream.write(frame[offset:])
        if (
            isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
            or written > len(frame) - offset
        ):
            raise ValueError(f"{label} write failed")
        offset += written
    stream.flush()
```

- [ ] **Step 4: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame -v`
Expected: 全件 OK

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image -v`
Expected: 17 tests OK（新 subpackage が image source set に入り、host-only module を含まない）

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/__init__.py src/agent_container/broker/frame.py tests/container/test_broker_frame.py
git commit -m "feat: add the broker kernel frame codec

Length-prefixed JSON framing with per-schema JSON options, duplicate
key and non-finite constant rejection, exact field sets, bounded
reads, and partial-write retry. No broker uses it yet.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 4: handover protocol を `broker.frame` に乗せ替える

**Files:**
- Modify: `src/agent_container/handover_broker_protocol.py`（全面書き換え。公開名は維持）

**Interfaces:**
- Consumes: Task 3 の `FrameSchema`、`JsonOptions`、`encode_frame`、`decode_frame`、`read_frame`
- Produces: 従来どおり `HandoverRequest`、`HandoverResponse`、`PROTOCOL_VERSION`、`MAX_REQUEST_BYTES`、`MAX_DOCUMENT_BYTES`、`encode_request_frame`、`decode_request_frame`、`encode_response_frame`、`decode_response_frame`、`read_request_frame`、`read_response_frame`

- [ ] **Step 1: 乗せ替え前の baseline を記録**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_protocol tests.container.test_broker_frame_golden -v 2>&1 | tail -3`
Expected: OK（protocol 13 件 + golden 4 件）

- [ ] **Step 2: protocol module を書き換える**

`src/agent_container/handover_broker_protocol.py` 全体:

```python
from dataclasses import dataclass
import os
from typing import Any
from typing import BinaryIO

from agent_container.broker.frame import FrameSchema
from agent_container.broker.frame import JsonOptions
from agent_container.broker.frame import decode_frame
from agent_container.broker.frame import encode_frame
from agent_container.broker.frame import read_frame


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_DOCUMENT_BYTES = 65_536

_REQUEST_FIELDS = frozenset(
    {"version", "capability", "project_id", "operation", "title", "body"}
)
_RESPONSE_FIELDS = frozenset({"version", "status", "path", "code"})
_RESPONSE_STATUSES = frozenset({"ok", "denied", "error"})
_RESPONSE_CODES = frozenset(
    {
        "authentication",
        "schema",
        "size",
        "content-policy",
        "filesystem-boundary",
        "write",
        "unavailable",
    }
)
_REQUEST_OPERATION = "create"
_JSON = JsonOptions(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
_REQUEST_SCHEMA = FrameSchema(
    label="handover broker request",
    stream_label="handover broker stream",
    fields=_REQUEST_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=_JSON,
)
_RESPONSE_SCHEMA = FrameSchema(
    label="handover broker response",
    stream_label="handover broker stream",
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_REQUEST_BYTES,
    json=_JSON,
)


@dataclass(frozen=True)
class HandoverRequest:
    version: int
    capability: str
    project_id: str
    operation: str
    title: str
    body: str


@dataclass(frozen=True)
class HandoverResponse:
    version: int
    status: str
    path: str
    code: str


def _validate_string(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("handover broker schema is invalid")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("handover broker schema is invalid")
    return value


def _validate_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != PROTOCOL_VERSION:
        raise ValueError("handover broker schema is invalid")
    return value


def _validate_operation(value: Any) -> str:
    operation = _validate_string(value)
    if operation != _REQUEST_OPERATION:
        raise ValueError("handover broker request schema is invalid")
    return operation


def _request_from_values(values: dict[str, Any]) -> HandoverRequest:
    return HandoverRequest(
        version=_validate_version(values["version"]),
        capability=_validate_string(values["capability"]),
        project_id=_validate_string(values["project_id"]),
        operation=_validate_operation(values["operation"]),
        title=_validate_string(values["title"]),
        body=_validate_string(values["body"]),
    )


def encode_request_frame(request: HandoverRequest) -> bytes:
    if not isinstance(request, HandoverRequest):
        raise ValueError("handover broker request is invalid")
    payload_values = {
        "version": _validate_version(request.version),
        "capability": _validate_string(request.capability),
        "project_id": _validate_string(request.project_id),
        "operation": _validate_operation(request.operation),
        "title": _validate_string(request.title),
        "body": _validate_string(request.body),
    }
    return encode_frame(_REQUEST_SCHEMA, payload_values)


def decode_request_frame(data: bytes) -> tuple[HandoverRequest, int]:
    decoded, consumed = decode_frame(_REQUEST_SCHEMA, data)
    return _request_from_values(decoded), consumed


def _validate_response(response: HandoverResponse) -> tuple[int, str, str, str]:
    if not isinstance(response, HandoverResponse):
        raise ValueError("handover broker response is invalid")
    version = _validate_version(response.version)
    status = _validate_string(response.status)
    path = _validate_string(response.path)
    code = _validate_string(response.code)
    if status not in _RESPONSE_STATUSES:
        raise ValueError("handover broker response schema is invalid")
    if status == "ok":
        if not path or not os.path.isabs(path) or code:
            raise ValueError("handover broker response schema is invalid")
    elif path or code not in _RESPONSE_CODES:
        raise ValueError("handover broker response schema is invalid")
    return version, status, path, code


def encode_response_frame(response: HandoverResponse) -> bytes:
    version, status, path, code = _validate_response(response)
    return encode_frame(
        _RESPONSE_SCHEMA,
        {"version": version, "status": status, "path": path, "code": code},
    )


def _response_from_values(values: dict[str, Any]) -> HandoverResponse:
    response = HandoverResponse(
        version=values["version"],
        status=values["status"],
        path=values["path"],
        code=values["code"],
    )
    _validate_response(response)
    return response


def decode_response_frame(data: bytes) -> tuple[HandoverResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    return _response_from_values(decoded), consumed


def read_request_frame(stream: BinaryIO) -> HandoverRequest:
    return _request_from_values(read_frame(_REQUEST_SCHEMA, stream))


def read_response_frame(stream: BinaryIO) -> HandoverResponse:
    return _response_from_values(read_frame(_RESPONSE_SCHEMA, stream))
```

注意点（振る舞い保存の根拠）:
- 旧 `_decode_json` は内側の ValueError を `"handover broker {kind} JSON is invalid"` に包み直していた。kernel の `decode_frame` も同じ包み直しをするので message は一致する。
- 旧 `decode_response_frame` は `HandoverResponse(...)` を作ってから `_validate_response` していた。`_response_from_values` はその順序を保つ。
- 旧 `read_request_frame` は `_read_exact → length check → decode → consumed check` の順。kernel `read_frame` も同順で、`consumed` 不一致時の message `"handover broker request frame is invalid"` も一致する。

- [ ] **Step 3: 既存 test と golden を実行**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_protocol tests.container.test_broker_frame_golden tests.container.test_handover_broker_transport tests.container.test_handover_broker tests.container.test_handover_broker_runtime tests.container.test_handover_writer tests.container.test_host_handover -v 2>&1 | tail -5`
Expected: 全件 OK。`git diff --stat tests/` に既存 handover test file が現れないこと。

- [ ] **Step 4: lint**

Run: `bin/lint`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/handover_broker_protocol.py
git commit -m "refactor: frame handover protocol with the broker kernel

Field validation and the fixed schema stay in the handover module;
length-prefixed JSON encoding, decoding, and bounded stream reads
move to agent_container.broker.frame. Golden bytes and the existing
protocol tests are unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 5: `broker/capability.py`（TDD）

**Files:**
- Create: `src/agent_container/broker/capability.py`
- Test: `tests/container/test_broker_capability.py`

**Interfaces:**
- Produces: 「Interfaces」節の `CAPABILITY_PATTERN`、`validate_exact_path`、`read_capability`、`validate_socket`、`connect_unix`

- [ ] **Step 1: failing test を書く**

`tests/container/test_broker_capability.py`:

```python
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.capability import connect_unix
from agent_container.broker.capability import read_capability
from agent_container.broker.capability import validate_exact_path
from agent_container.broker.capability import validate_socket


LABEL = "test capability"


class ValidateExactPathTest(unittest.TestCase):
    def test_accepts_only_absolute_resolved_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target"
            target.write_text("x", encoding="ascii")
            self.assertEqual(validate_exact_path(target, label=LABEL), target)

            link = root / "link"
            link.symlink_to(target)
            for path in (Path("relative"), link, root / "missing", root / "." / "target"):
                with self.subTest(path=path), self.assertRaises(ValueError) as raised:
                    validate_exact_path(path, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")


class ReadCapabilityTest(unittest.TestCase):
    def test_pattern_is_exactly_43_url_safe_characters(self) -> None:
        self.assertIsNotNone(CAPABILITY_PATTERN.fullmatch("A" * 43))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 42))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 44))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 42 + "+"))

    def test_reads_only_exact_private_current_user_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            capability = root / "capability"
            capability.write_text("c" * 43 + "\n", encoding="ascii")
            capability.chmod(0o600)

            self.assertEqual(read_capability(capability, label=LABEL), "c" * 43)

            link = root / "link"
            link.symlink_to(capability)
            for path in (Path("capability"), link):
                with self.subTest(path=path), self.assertRaises(ValueError) as raised:
                    read_capability(path, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")

            capability.chmod(0o644)
            with self.assertRaises(ValueError):
                read_capability(capability, label=LABEL)
            capability.chmod(0o600)
            with mock.patch("os.getuid", return_value=os.getuid() + 1), self.assertRaises(
                ValueError
            ):
                read_capability(capability, label=LABEL)

            directory_path = root / "directory"
            directory_path.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                read_capability(directory_path, label=LABEL)

    def test_rejects_wrong_size_missing_newline_and_non_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            for body in (
                "c" * 43,
                "c" * 44 + "\n",
                "c" * 42 + "\n\n",
                "é" * 21 + "c\n",
                "c" * 42 + "+\n",
            ):
                capability.write_bytes(body.encode("utf-8"))
                capability.chmod(0o600)
                with self.subTest(body=body), self.assertRaises(ValueError) as raised:
                    read_capability(capability, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")

    def test_rejects_path_replaced_after_descriptor_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            capability.write_text("c" * 43 + "\n", encoding="ascii")
            capability.chmod(0o600)
            real_open = os.open

            def open_then_replace(path: Path, flags: int) -> int:
                descriptor = real_open(path, flags)
                capability.unlink()
                capability.write_text("d" * 43 + "\n", encoding="ascii")
                capability.chmod(0o600)
                return descriptor

            with mock.patch("os.open", side_effect=open_then_replace), self.assertRaises(
                ValueError
            ):
                read_capability(capability, label=LABEL)

    def test_opens_with_nonblocking_nofollow_flags_and_rejects_fifo(self) -> None:
        if not hasattr(os, "O_NONBLOCK") or not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("platform does not expose safe Unix open flags")
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            os.mkfifo(capability, mode=0o600)
            real_open = os.open
            seen_flags: list[int] = []

            def checked_open(path: Path, flags: int) -> int:
                seen_flags.append(flags)
                return real_open(path, flags)

            with mock.patch("os.open", side_effect=checked_open), self.assertRaises(ValueError):
                read_capability(capability, label=LABEL)
            self.assertEqual(len(seen_flags), 1)
            self.assertTrue(seen_flags[0] & os.O_NONBLOCK)
            self.assertTrue(seen_flags[0] & os.O_NOFOLLOW)


class ValidateSocketTest(unittest.TestCase):
    def test_requires_socket_type_private_mode_and_current_user(self) -> None:
        path = Path("/run/agent-test/broker.sock")
        valid = os.stat_result(
            (stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)
        )
        with mock.patch.object(Path, "stat", return_value=valid):
            self.assertEqual(validate_socket(path, label="test socket"), path)
        invalid = (
            os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0)),
        )
        for metadata in invalid:
            with self.subTest(mode=metadata.st_mode, uid=metadata.st_uid), mock.patch.object(
                Path, "stat", return_value=metadata
            ), self.assertRaises(ValueError) as raised:
                validate_socket(path, label="test socket")
            self.assertEqual(str(raised.exception), "test socket is invalid")

    def test_does_not_resolve_the_path_itself(self) -> None:
        path = Path("/run/agent-test/broker.sock")
        valid = os.stat_result(
            (stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)
        )
        with mock.patch.object(Path, "stat", return_value=valid), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("must not resolve")
        ):
            self.assertEqual(validate_socket(path, label="test socket"), path)


class FakeSocket:
    def __init__(self, connect_error: OSError | None = None) -> None:
        self.connect_error = connect_error
        self.timeout: float | None = None
        self.connected: str | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = path

    def close(self) -> None:
        self.closed = True


class ConnectUnixTest(unittest.TestCase):
    def test_creates_unix_stream_socket_sets_timeout_and_connects(self) -> None:
        created: list[tuple[object, object]] = []
        fake = FakeSocket()

        def factory(family: object, kind: object) -> FakeSocket:
            created.append((family, kind))
            return fake

        client = connect_unix(Path("/run/agent-test/broker.sock"), timeout=30, socket_factory=factory)
        self.assertIs(client, fake)
        self.assertEqual(created, [(socket.AF_UNIX, socket.SOCK_STREAM)])
        self.assertEqual(fake.timeout, 30)
        self.assertEqual(fake.connected, "/run/agent-test/broker.sock")
        self.assertFalse(fake.closed)

    def test_closes_socket_and_reraises_when_connect_fails(self) -> None:
        fake = FakeSocket(connect_error=FileNotFoundError("private-socket-marker"))
        with self.assertRaises(FileNotFoundError):
            connect_unix(Path("/run/agent-test/broker.sock"), timeout=30, socket_factory=lambda *_: fake)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_capability -v`
Expected: `ModuleNotFoundError: No module named 'agent_container.broker.capability'`

- [ ] **Step 3: 実装**

`src/agent_container/broker/capability.py`:

```python
"""Container-side validation of broker runtime paths and capabilities."""

import os
from pathlib import Path
import re
import socket
import stat
from typing import Any
from typing import Callable


CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CAPABILITY_FILE_BYTES = 44
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", None)


def validate_exact_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} is invalid") from None
    if resolved != path:
        raise ValueError(f"{label} is invalid")
    return resolved


def read_capability(path: Path, *, label: str) -> str:
    if not path.is_absolute() or _NONBLOCK is None:
        raise ValueError(f"{label} is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError:
        raise ValueError(f"{label} is invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size != _CAPABILITY_FILE_BYTES
        ):
            raise ValueError(f"{label} is invalid")

        output = bytearray()
        while len(output) < _CAPABILITY_FILE_BYTES + 1:
            chunk = os.read(descriptor, _CAPABILITY_FILE_BYTES + 1 - len(output))
            if not chunk:
                break
            output.extend(chunk)
        body = bytes(output)

        try:
            resolved = path.resolve(strict=True)
            path_metadata = path.lstat()
        except (OSError, RuntimeError):
            raise ValueError(f"{label} is invalid") from None
        if (
            resolved != path
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise ValueError(f"{label} is invalid")
    except OSError:
        raise ValueError(f"{label} is invalid") from None
    finally:
        os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError(f"{label} is invalid") from None
    if (
        CAPABILITY_PATTERN.fullmatch(capability) is None
        or body != (capability + "\n").encode("ascii")
    ):
        raise ValueError(f"{label} is invalid")
    return capability


def validate_socket(path: Path, *, label: str) -> Path:
    metadata = path.stat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError(f"{label} is invalid")
    return path


def connect_unix(
    path: Path,
    *,
    timeout: float,
    socket_factory: Callable[..., Any] = socket.socket,
) -> Any:
    client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
    except BaseException:
        client.close()
        raise
    return client
```

- [ ] **Step 4: PASS を確認**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_broker_capability -v`
Expected: 全件 OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/capability.py tests/container/test_broker_capability.py
git commit -m "feat: add the broker kernel capability helpers

Exact path, private capability file, private socket checks and a
Unix socket connector for container-side broker clients. Semantics
match the handover client, the strictest of the four brokers.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 6: handover client を `broker.capability` / `frame.write_all` に乗せ替える

**Files:**
- Modify: `src/agent_container/handover_broker_client.py`

**Interfaces:**
- Consumes: Task 5 の `validate_exact_path`、`read_capability`、`validate_socket`、`connect_unix`、`CAPABILITY_PATTERN`; Task 3 の `write_all`
- Produces: 従来どおり `HandoverBrokerClient`、`read_handover_capability`、`validate_handover_socket`、`run`、`main`。module 属性 `os`、`_validate_exact_path`、`_CAPABILITY`、`_SOCKET_TIMEOUT_SECONDS` を維持（既存 test が `mock.patch("agent_container.handover_broker_client.os.open")`、`...os.getuid`、`..._validate_exact_path`、`...read_handover_capability`、`...validate_handover_socket` を使う）

- [ ] **Step 1: 乗せ替え前の baseline**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_client -v 2>&1 | tail -3`
Expected: OK（10 件）

- [ ] **Step 2: client の import と検証関数を差し替える**

`src/agent_container/handover_broker_client.py` の先頭〜`validate_handover_socket` までを次に置き換える（`HandoverBrokerClient` 以降は Step 3 で変更）:

```python
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import sys
from typing import BinaryIO, Mapping, Sequence, TextIO

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.capability import connect_unix
from agent_container.broker.capability import read_capability
from agent_container.broker.capability import validate_exact_path
from agent_container.broker.capability import validate_socket
from agent_container.broker.frame import write_all
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import read_response_frame
from agent_container.state import validate_project_id


_CAPABILITY = CAPABILITY_PATTERN
_SOCKET_TIMEOUT_SECONDS = 30


def _validate_exact_path(path: Path) -> Path:
    return validate_exact_path(path, label="handover broker runtime path")


def read_handover_capability(path: Path) -> str:
    return read_capability(path, label="handover broker capability file")


def validate_handover_socket(path: Path) -> Path:
    return validate_socket(_validate_exact_path(path), label="handover broker socket")
```

削除するもの: `import re`、`import stat`、`_NOFOLLOW`、`_NONBLOCK`、旧 `_validate_exact_path`/`read_handover_capability`/`validate_handover_socket` の本体。**`import os` と `import socket` は残す**（`os` は既存 test の patch 対象、`socket` は `socket_factory: object = socket.socket` の既定値）。

- [ ] **Step 3: `HandoverBrokerClient.create` の接続と送信を kernel に委ねる**

`create` の `frame = encode_request_frame(request)` 以降を次に置き換える:

```python
        frame = encode_request_frame(request)

        client = connect_unix(
            self.socket_path,
            timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_factory=self.socket_factory,
        )
        stream: BinaryIO | None = None
        try:
            stream = client.makefile("rwb", buffering=0)
            write_all(stream, frame, label="handover broker request")
            response = read_response_frame(stream)
            if response.status != "ok":
                raise RuntimeError("handover broker request failed")
            return response.path
        finally:
            if stream is not None:
                stream.close()
            client.close()
```

`_parser`、`_self_check`、`run`、`main` は変更しない（`_self_check` は `_CAPABILITY.fullmatch` を引き続き使える）。

- [ ] **Step 4: 既存 client test を実行（変更なしで PASS すること）**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_client -v`
Expected: 10 件 OK。特に確認する test と、通る理由:
- `test_reads_only_exact_private_current_user_capability`: `mock.patch("agent_container.handover_broker_client.os.getuid")` は module 属性 `os`（= `os` module そのもの）の `getuid` を差し替えるので、kernel 内の `os.getuid()` にも効く。
- `test_socket_requires_exact_private_current_user_socket`: `_validate_exact_path` を client module 名で patch → wrapper 経由で kernel の `validate_socket` は resolve しないので、`Path.stat` の patch だけで成立する。
- `test_capability_rejects_path_replaced_after_descriptor_open` / `test_capability_opens_fifo_with_nonblocking_nofollow_flags`: `...handover_broker_client.os.open` の patch は `os.open` 全体に効く。
- `test_denied_error_and_unavailable_fail_closed_without_secret_echo`: connect 失敗時に `connect_unix` が socket を close して `OSError` を再送出する。
- `test_create_retries_until_the_entire_request_frame_is_sent` / `test_create_rejects_invalid_request_write_progress`: `write_all` の retry と失敗判定は旧 loop と同一。

もし 1 件でも FAIL したら **test を変えずに** 実装側を直す。直せない場合は止めて報告。

- [ ] **Step 5: 関連 test と lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_client tests.container.test_agentctl tests.container.test_podman -v 2>&1 | tail -3`
Expected: OK

Run: `PYTHONPATH=src python3 -m unittest tests.integration.test_handover_broker_socket -v 2>&1 | tail -3`
Expected: OK（実 UNIX socket で client ↔ runtime を往復する。skip される場合は理由を記録）

Run: `bin/lint`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/handover_broker_client.py
git commit -m "refactor: back the handover client with the broker kernel

Path, capability, and socket validation plus the Unix connect and
full-frame write now come from agent_container.broker. The module
keeps its public names and patch points so the existing client
tests run unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
```

---

### Task 7: docs（spec 訂正、CHANGELOG）と全体検証、PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: spec の事実を訂正**

`docs/superpowers/specs/2026-09-04-broker-kernel-design.md` の「stage 1で変えないもの › wire形式」の実測値 4 行を次に置き換える:

```markdown
  - handover: request／response とも `ensure_ascii=False, allow_nan=False, separators=(",", ":")`、utf-8
  - family: request／response とも `ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True`、utf-8
  - github: request は `ensure_ascii=False, allow_nan=False, separators=(",", ":")` を utf-8、response は `separators=(",", ":"), sort_keys=True` を ascii（`ensure_ascii`・`allow_nan` は既定）
  - egress: request は `allow_nan=False, separators=(",", ":"), sort_keys=True` を ascii、response は `separators=(",", ":"), sort_keys=True` を ascii
  - request と response で options が異なる broker があるため、`FrameSchema` は request 用と response 用を別に宣言する（6-1 で実装済み）。
```

同節の「`.containerignore`は`!src/**`で`src/`全体を許可しており追加設定は不要」の直後に次を追加:

```markdown
ただし`tests/container/test_image.py`の`test_effective_image_tree_imports_host_entrypoints_without_host_modules`はtop-levelの`.py`だけを仮image treeへcopyしていたため、6-1でsubdirectoryを含めてcopyするよう修正した。これはbroker testではなくimage contract testのinfra修正である。
```

- [ ] **Step 2: CHANGELOG**

`CHANGELOG.md` の `## [Unreleased]` › `### Added` の末尾に追加:

```markdown
- Phase 6 stage 1の最初の乗せ替えとして、共通broker kernel package `agent_container/broker/`に`frame`（length-prefixed JSON frame codecとstream helper）と`capability`（container側のexact path・capability file・socket検証と接続）を追加し、handover brokerのprotocolとcontainer側clientをその上に移しました。wire形式、audit、既存のhandover testは変更していません。kernel化前のencoderで生成したgolden byte fixtureを`tests/container/test_broker_frame_golden.py`に固定しました。
```

- [ ] **Step 3: 全体検証**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | tail -3
PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | tail -3
bin/lint
git diff --stat main -- tests/
```
Expected: container 996 + 新規（golden 4、frame 約 14、capability 約 10）が OK、codex 44 OK、lint pass。`git diff --stat main -- tests/` に現れる file は `test_broker_frame_golden.py`、`test_broker_frame.py`、`test_broker_capability.py`、`test_image.py` の 4 つだけ。

- [ ] **Step 4: Commit と push、PR**

```bash
git add docs/superpowers/specs/2026-09-04-broker-kernel-design.md CHANGELOG.md
git commit -m "docs: record the broker kernel 6-1 landing and correct spec facts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011SkLNBRr8g2udHjj4nNRKm"
git push -u origin feat/broker-kernel-frame
```

PR 本文（`gh pr create`）に含めること:
- Summary: 何を kernel に移し、何を handover に残したか
- Security boundary: 触るのは frame codec と container 側 path/capability 検証だけ。socket topology、mount、capability の粒度、audit、Podman flag は変更なし
- Verification: 上の test 数、`git diff --stat main -- tests/` の 4 file、golden fixture の出自 commit `b1198d1`
- 既存 handover test 6 file が無変更であること
- 末尾: `🤖 Generated with [Claude Code](https://claude.com/claude-code)` と session URL

- [ ] **Step 5: CI を待ち、pass したら利用者に merge 判断を仰ぐ**

`gh pr checks <n> --watch`。merge 後は worktree と branch を削除し、`main` を fast-forward する（repo 規約）。

---

## Verification（end-to-end）

1. golden: `test_broker_frame_golden.py` が kernel 化の前後で同じ byte 列を要求する（Task 1 で現行 encoder に対して PASS、Task 4 以降も PASS）。
2. 既存 test 不変: `git diff --stat main -- tests/` に handover の既存 6 file が現れない。
3. 実 socket: `tests/integration/test_handover_broker_socket.py` が実 UNIX socket で client → runtime → response を往復する。
4. image contract: `tests/container/test_image.py` が `broker/` を image source set に含め、host-only module を含まないことを確認する。
5. CI: Unit tests と Podman integration（後者は image を build して `agent-handover` を含む container 側 entrypoint を実行する）。
6. 実 host smoke は 6-6 でまとめて行う（spec の方針）。6-1 単体では行わない。

## Self-review 記録

- Spec coverage（6-1 の範囲）: frame.py → Task 3、capability.py → Task 5、handover protocol → Task 4、handover client → Task 6、golden fixture → Task 1、既存 test 不変 → 各 task の Step、spec 訂正 → Task 7。`broker/runtime.py`・`audit.py`・`readiness.py` は 6-2 の範囲で本 plan 外。
- 型の一貫性: `FrameSchema(label, stream_label, fields, max_bytes, json)` と `JsonOptions(ensure_ascii, allow_nan, sort_keys, separators, encoding)` を Task 3 で定義し、Task 4 でその引数名どおりに使う。`read_capability(path, *, label)`・`validate_socket(path, *, label)`・`validate_exact_path(path, *, label)`・`connect_unix(path, *, timeout, socket_factory)`・`write_all(stream, frame, *, label)` を Task 5/3 で定義し、Task 6 で同名・同引数で使う。
- Placeholder: なし。
