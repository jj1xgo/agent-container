import io
import json
import sys
import unittest
from unittest import mock

from agent_container import github_broker_protocol as protocol
from agent_container.broker.frame import FrameJsonError
from agent_container.broker.frame import FrameSchemaError
from agent_container.broker.frame import StreamError
from agent_container.github_broker_protocol import BrokerResponse


def frame(body):
    return len(body).to_bytes(4, "big") + body


class GitHubKernelCodecTest(unittest.TestCase):
    def test_response_decoding_uses_kernel_error_kinds(self):
        cases = (
            (b"x", "broker response frame is incomplete"),
            (b"\x00\x00\x00\x00", "broker response frame size is invalid"),
            (b"\x00\x00\x00\x02x", "broker response frame is incomplete"),
            (frame(b'{"version":1,"version":1,"status":"ok"}'), "broker response JSON is invalid"),
            (frame(b'{"version":Infinity,"status":"ok"}'), "broker response JSON is invalid"),
            (frame(b'{"version":true,"status":"ok"}'), "broker response schema is invalid"),
            (frame(b'{"version":1,"status":[]}'), "broker response schema is invalid"),
            (frame(b'{"version":2,"status":"ok"}'), "broker response schema is invalid"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "^" + message + "$"):
                protocol.decode_response_frame(raw)
        response, consumed = protocol.decode_response_frame(frame(b'{"status":"ok","version":1}') + b"tail")
        self.assertEqual(response, BrokerResponse(1, "ok"))
        self.assertEqual(consumed, 4 + len(b'{"status":"ok","version":1}'))

    def test_request_encoding_reports_invalid_payloads_as_value_errors(self):
        with self.assertRaises(FrameSchemaError) as raised:
            protocol.encode_request_frame(
                protocol.BrokerRequest(1, "A" * 43, "demo", 1, "issue-list", {"x": object()})
            )
        self.assertEqual(str(raised.exception), "broker request is invalid")

    def test_stream_failures_are_stream_errors(self):
        stream = mock.Mock()
        stream.read.side_effect = OSError("synthetic-stream-error")
        with self.assertRaises(StreamError) as raised:
            protocol.read_request_frame(stream)
        self.assertEqual(str(raised.exception), "broker stream is invalid")
        with self.assertRaisesRegex(StreamError, "^broker stream is incomplete$"):
            protocol.read_response_frame(io.BytesIO(b"\x00\x00\x00\x05ab"))

    def test_request_json_errors_keep_the_github_decoder(self):
        with self.assertRaisesRegex(ValueError, "^broker request JSON is invalid$"):
            protocol.decode_request_frame(frame(b'{"version":1,"version":1}'))
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

    def test_chunk_streams_retry_short_writes_and_honor_initial_eof(self):
        class ShortWriter(io.BytesIO):
            def write(self, body):
                return super().write(body[:1])

        stream = ShortWriter()
        self.assertEqual(protocol.write_chunk_stream(stream, (b"ab",)), 2)
        self.assertEqual(stream.getvalue(), bytes.fromhex("00000002616200000000"))
        self.assertEqual(
            list(protocol.iter_chunk_stream(io.BytesIO(), maximum_total=0, allow_initial_eof=True)),
            [],
        )
        with self.assertRaisesRegex(StreamError, "^broker stream is incomplete$"):
            list(protocol.iter_chunk_stream(io.BytesIO(b"\x00"), maximum_total=0, allow_initial_eof=True))
        with self.assertRaises(FrameJsonError):
            protocol.decode_response_frame(frame(b"{"))
