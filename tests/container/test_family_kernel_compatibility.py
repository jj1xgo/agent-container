"""Family guarantees that differ from generic kernel defaults."""

from io import BytesIO
import struct
import unittest

from agent_container import family_intake_protocol as protocol
from tests.container.broker_family_golden_support import example_request
from tests.container.broker_family_golden_support import example_response


class BytesSubclass(bytes):
    pass


class IntSubclass(int):
    pass


class FamilyKernelCompatibilityTest(unittest.TestCase):
    def test_decode_requires_exact_bytes_before_frame_or_json_processing(self):
        for kind, encoder, decoder, value in self._codecs():
            frame = encoder(value)
            for invalid in (BytesSubclass(frame), bytearray(frame), memoryview(frame), None):
                with self.subTest(kind=kind, type=type(invalid).__name__):
                    with self.assertRaises(ValueError) as raised:
                        decoder(invalid)
                    self.assertEqual(str(raised.exception), f"family intake {kind} frame is incomplete")

    def test_json_and_schema_failures_preserve_family_messages(self):
        for kind, _encoder, decoder, _value in self._codecs():
            for body in (b"!", b"\xff", b'{"k":1,"k":2}', b'{"k":NaN}', b'{"k":Infinity}'):
                with self.subTest(kind=kind, body=body):
                    with self.assertRaises(ValueError) as raised:
                        decoder(struct.pack(">I", len(body)) + body)
                    self.assertEqual(str(raised.exception), f"family intake {kind} JSON is invalid")
                    self.assertTrue(raised.exception.__suppress_context__)
            for body in (b"[]", b"null", b"1", b'"x"', b"{}"):
                with self.subTest(kind=kind, body=body):
                    with self.assertRaises(ValueError) as raised:
                        decoder(struct.pack(">I", len(body)) + body)
                    self.assertEqual(str(raised.exception), f"family intake {kind} schema is invalid")

    def test_response_size_limit_counts_the_header(self):
        response = protocol.FamilyIntakeResponse(1, "pending", "r", 1)
        initial = len(protocol.encode_response_frame(response))
        exponent = 1024 - initial
        boundary = protocol.FamilyIntakeResponse(1, "pending", "r", 10 ** exponent)
        raw = protocol.encode_response_frame(boundary)
        self.assertEqual(len(raw), 1024)
        self.assertEqual(protocol.decode_response_frame(raw), (boundary, 1024))
        oversized = protocol.FamilyIntakeResponse(1, "pending", "r", 10 ** (exponent + 1))
        with self.assertRaisesRegex(ValueError, "family intake response is too large"):
            protocol.encode_response_frame(oversized)
        with self.assertRaisesRegex(ValueError, "family intake response frame size is invalid"):
            protocol.decode_response_frame(struct.pack(">I", 1021) + raw[4:] + b" ")

    def test_read_rejects_bytes_subclasses_and_sanitizes_stream_exceptions(self):
        class Reader:
            def __init__(self, result):
                self.result = result

            def read(self, _size):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        for result in (BytesSubclass(b"\0"), b"", None, b"12345"):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, "family intake stream is incomplete"):
                protocol.read_response_frame(Reader(result))
        for error in (OSError("private marker"), TypeError("private marker"), ValueError("private marker")):
            with self.subTest(error=type(error)), self.assertRaisesRegex(ValueError, "family intake stream is invalid"):
                protocol.read_response_frame(Reader(error))

    def test_write_rejects_nonexact_counts_and_sanitizes_write_and_flush(self):
        class Writer:
            def __init__(self, result=None, *, flush_error=None):
                self.result = result
                self.flush_error = flush_error
                self.flush_called = False

            def write(self, body):
                if isinstance(self.result, Exception):
                    raise self.result
                return len(body) if self.flush_error else self.result

            def flush(self):
                self.flush_called = True
                if self.flush_error:
                    raise self.flush_error

        for count in (IntSubclass(1), True, None, 0, -1, 100000):
            stream = Writer(count)
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "family intake stream write failed"):
                protocol.write_response_frame(stream, example_response())
            self.assertFalse(stream.flush_called)
        for error_type in (OSError, TypeError, ValueError):
            for phase in ("write", "flush"):
                error = error_type("private marker")
                stream = Writer(error) if phase == "write" else Writer(flush_error=error)
                with self.subTest(error_type=error_type, phase=phase):
                    with self.assertRaisesRegex(ValueError, "family intake stream is invalid"):
                        protocol.write_response_frame(stream, example_response())

    def test_split_writes_keep_complete_frames_and_one_flush(self):
        class SplitWriter(BytesIO):
            flushes = 0

            def write(self, body):
                return super().write(body[:1])

            def flush(self):
                self.flushes += 1
                super().flush()

        stream = SplitWriter()
        protocol.write_request_frame(stream, example_request())
        self.assertEqual(stream.getvalue(), protocol.encode_request_frame(example_request()))
        self.assertEqual(stream.flushes, 1)

    def _codecs(self):
        return (
            ("request", protocol.encode_request_frame, protocol.decode_request_frame, example_request()),
            ("response", protocol.encode_response_frame, protocol.decode_response_frame, example_response()),
        )
