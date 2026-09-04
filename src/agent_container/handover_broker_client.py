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


@dataclass(frozen=True)
class HandoverBrokerClient:
    socket_path: Path
    capability_path: Path
    project_id: str
    socket_factory: object = socket.socket

    def create(self, title: str, body: bytes) -> str:
        if not isinstance(title, str) or not isinstance(body, bytes):
            raise ValueError("handover broker request is invalid")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("handover broker request is too large")
        try:
            decoded_body = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("handover broker request is invalid") from None
        project_id = validate_project_id(self.project_id)
        validate_handover_socket(self.socket_path)
        capability = read_handover_capability(self.capability_path)
        request = HandoverRequest(
            version=PROTOCOL_VERSION,
            capability=capability,
            project_id=project_id,
            operation="create",
            title=title,
            body=decoded_body,
        )
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-handover")
    parser.add_argument("--self-check", action="store_true")
    commands = parser.add_subparsers(dest="operation")
    create = commands.add_parser("create")
    create.add_argument("--title", required=True)
    return parser


def _self_check() -> bool:
    return (
        PROTOCOL_VERSION == 1
        and MAX_REQUEST_BYTES == 65_536
        and _SOCKET_TIMEOUT_SECONDS == 30
        and _CAPABILITY.fullmatch("A" * 43) is not None
        and _CAPABILITY.fullmatch("A" * 42) is None
        and _CAPABILITY.fullmatch("A" * 44) is None
    )


def run(
    argv: Sequence[str],
    environment: Mapping[str, str],
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    client_factory=HandoverBrokerClient,  # type: ignore[no-untyped-def]
) -> int:
    try:
        options = _parser().parse_args(argv)
        if options.self_check:
            if options.operation is not None:
                return 1
            return 0 if _self_check() else 1
        if options.operation != "create":
            raise ValueError("handover broker operation is required")
        socket_value = environment.get("AGENT_HANDOVER_BROKER_SOCKET", "")
        capability_value = environment.get("AGENT_HANDOVER_BROKER_CAPABILITY", "")
        project_id = environment.get("AGENT_PROJECT_ID", "")
        if not socket_value or not capability_value or not project_id:
            raise ValueError("handover broker environment is incomplete")
        body = stdin.read(MAX_REQUEST_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
            raise ValueError("handover broker stdin is invalid")
        client = client_factory(
            socket_path=Path(socket_value),
            capability_path=Path(capability_value),
            project_id=project_id,
        )
        path = client.create(options.title, body)
        stdout.write(path + "\n")
        return 0
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        print("error: handover broker request failed", file=stderr)
        return 1


def main() -> int:
    return run(sys.argv[1:], os.environ, sys.stdin.buffer, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
