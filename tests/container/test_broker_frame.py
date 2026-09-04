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
