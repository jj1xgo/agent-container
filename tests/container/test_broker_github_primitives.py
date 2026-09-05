import io
import struct
import unittest
from unittest import mock

from agent_container.broker import frame as kernel


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
