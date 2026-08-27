import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import socket
import stat
import sys
from typing import BinaryIO, Mapping, Sequence, TextIO

from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import read_response_frame
from agent_container.state import validate_project_id


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SOCKET_TIMEOUT_SECONDS = 30
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _validate_exact_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("handover broker runtime path is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("handover broker runtime path is invalid") from None
    if resolved != path:
        raise ValueError("handover broker runtime path is invalid")
    return resolved


def read_handover_capability(path: Path) -> str:
    path = _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_size != 44
    ):
        raise ValueError("handover broker capability file is invalid")
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        body = os.read(descriptor, 45)
    finally:
        os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError("handover broker capability file is invalid") from None
    if (
        _CAPABILITY.fullmatch(capability) is None
        or body != (capability + "\n").encode("ascii")
    ):
        raise ValueError("handover broker capability file is invalid")
    return capability


def validate_handover_socket(path: Path) -> Path:
    path = _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("handover broker socket is invalid")
    return path


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

        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
        stream: BinaryIO | None = None
        try:
            client.settimeout(_SOCKET_TIMEOUT_SECONDS)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            stream.write(frame)
            stream.flush()
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
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("create")
    create.add_argument("--title", required=True)
    return parser


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
