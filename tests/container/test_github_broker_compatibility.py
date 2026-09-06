import io
import json
import struct
import sys
import unittest
from unittest import mock

from agent_container import github_broker_protocol as protocol


def frame(body):
    return struct.pack(">I", len(body)) + body


class GitHubCompatibilityTest(unittest.TestCase):
    def test_response_legacy_validation(self):
        with self.assertRaisesRegex(ValueError, "^broker request JSON is invalid$"):
            protocol.decode_response_frame(
                frame(b'{"version":1,"version":1,"status":"ok"}')
            )
        result, _ = protocol.decode_response_frame(
            frame(b'{"version":true,"status":"ok"}')
        )
        self.assertIs(result.version, True)
        with self.assertRaises(TypeError):
            protocol.decode_response_frame(frame(b'{"version":1,"status":[]}'))
        for raw, message in (
            (b"x", "broker response frame is incomplete"),
            (b"\x00\x00\x00\x00", "broker response frame is invalid"),
            (b"\x00\x00\x00\x02x", "broker response frame is invalid"),
            (
                frame(b'{"version":Infinity,"status":"ok"}'),
                "broker response schema is invalid",
            ),
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                ValueError, "^" + message + "$"
            ):
                protocol.decode_response_frame(raw)

    def test_request_encoder_and_read_errors_escape(self):
        with self.assertRaises(TypeError):
            protocol.encode_request_frame(
                protocol.BrokerRequest(
                    1, "A" * 43, "demo", 1, "issue-list", {"x": object()}
                )
            )
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
        self.assertEqual(
            list(
                protocol.iter_chunk_stream(
                    io.BytesIO(), maximum_total=0, allow_initial_eof=True
                )
            ),
            [],
        )
        with self.assertRaisesRegex(ValueError, "broker stream is incomplete"):
            list(
                protocol.iter_chunk_stream(
                    io.BytesIO(b"\x00"), maximum_total=0, allow_initial_eof=True
                )
            )

