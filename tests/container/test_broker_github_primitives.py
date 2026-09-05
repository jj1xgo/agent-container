import io
import struct
import threading
import unittest
from unittest import mock

from agent_container.broker import audit as audit_kernel
from agent_container.broker import frame as kernel
from agent_container.broker import runtime as runtime_kernel


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

        with mock.patch.object(
            audit_kernel.os,
            "fsync",
            side_effect=lambda fd: events.append(("fsync", fd)),
        ):
            audit_kernel.append_text_record(Stream(), {"x": "日本語"})
        self.assertEqual(
            "".join(event[1] for event in events if event[0] == "write"),
            '{"x":"\\u65e5\\u672c\\u8a9e"}\n',
        )
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


class GitHubPrimitiveFrameTest(unittest.TestCase):
    def test_custom_decoder_keeps_error_and_header_gate(self):
        schema = kernel.FrameSchema(
            "demo", "demo stream", frozenset({"x"}), 8, kernel.JsonOptions()
        )
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
            kernel.decode_frame(
                schema, b"\x00\x00\x00\x02{}", json_decoder=lambda body: {}
            )

    def test_chunk_limit_is_checked_before_body_read(self):
        reader = mock.Mock(return_value=struct.pack(">I", 4))
        with self.assertRaisesRegex(ValueError, "unit stream is too large"):
            list(
                kernel.iter_chunk_stream(
                    read_bytes=reader,
                    maximum_total=3,
                    maximum_chunk=4,
                    label="unit stream",
                )
            )
        reader.assert_called_once_with(4, False)

    def test_chunk_limit_boundary_and_late_eof(self):
        reader = mock.Mock(
            side_effect=[struct.pack(">I", 4), b"abcd", b"\x00" * 4]
        )
        self.assertEqual(
            list(
                kernel.iter_chunk_stream(
                    read_bytes=reader,
                    maximum_total=4,
                    maximum_chunk=4,
                    label="unit stream",
                    allow_initial_eof=True,
                )
            ),
            [b"abcd"],
        )
        self.assertEqual(
            reader.call_args_list,
            [mock.call(4, True), mock.call(4, False), mock.call(4, False)],
        )
        reader = mock.Mock(return_value=struct.pack(">I", 5))
        with self.assertRaisesRegex(ValueError, "chunk is invalid"):
            list(
                kernel.iter_chunk_stream(
                    read_bytes=reader,
                    maximum_total=6,
                    maximum_chunk=4,
                    label="unit stream",
                )
            )
        reader.assert_called_once()

    def test_chunk_writer_keeps_framing_and_rejects_empty_chunk(self):
        stream = io.BytesIO()
        self.assertEqual(
            kernel.write_chunk_stream(
                stream, [b"ab"], maximum_chunk=2, label="unit stream"
            ),
            2,
        )
        self.assertEqual(stream.getvalue(), b"\x00\x00\x00\x02ab\x00\x00\x00\x00")
        with self.assertRaisesRegex(ValueError, "chunk is invalid"):
            kernel.write_chunk_stream(
                io.BytesIO(), [b""], maximum_chunk=2, label="unit stream"
            )


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
