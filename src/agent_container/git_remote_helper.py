from dataclasses import dataclass
import re
from typing import BinaryIO, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

from agent_container.state import Repository


MAX_STATELESS_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RECEIVE_PACK_REQUEST_BYTES = 256 * 1024 * 1024
MAX_STATELESS_PACKETS = 65_536
_HEX_HEADER = re.compile(br"^[0-9a-fA-F]{4}$")


class UploadPackTransport(Protocol):
    def discover(self) -> bytes: ...

    def rpc(self, request: bytes) -> Iterable[bytes]: ...


class ReceivePackTransport(Protocol):
    def discover(self) -> bytes: ...

    def push(self, request: bytes) -> Iterable[bytes]: ...


def parse_broker_repository_url(value: str, expected: str) -> Repository:
    if not isinstance(value, str) or not isinstance(expected, str):
        raise ValueError("broker repository URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "agent-broker"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
        or parsed.path.count("/") != 1
    ):
        raise ValueError("broker repository URL is invalid")
    repository = Repository.parse(f"{parsed.hostname}{parsed.path}")
    if repository.slug != expected:
        raise ValueError("broker repository is not allowed")
    return repository


def _read_exact(stream: BinaryIO, size: int, *, allow_initial_eof: bool = False) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if chunk == b"":
            if allow_initial_eof and not chunks:
                return b""
            raise ValueError("Git stateless request is incomplete")
        chunks.extend(chunk)
    return bytes(chunks)


def read_stateless_request(stream: BinaryIO) -> bytes | None:
    output = bytearray()
    packets = 0
    while True:
        header = _read_exact(stream, 4, allow_initial_eof=not output)
        if header == b"" and not output:
            return None
        if len(header) != 4 or _HEX_HEADER.fullmatch(header) is None:
            raise ValueError("Git stateless request is invalid")
        length = int(header, 16)
        output.extend(header)
        if len(output) > MAX_STATELESS_REQUEST_BYTES:
            raise ValueError("Git stateless request is too large")
        if length == 0:
            return bytes(output)
        if length == 1:
            packets += 1
            if packets > MAX_STATELESS_PACKETS:
                raise ValueError("Git stateless request has too many packets")
            continue
        if length < 4:
            raise ValueError("Git stateless request control packet is invalid")
        if length > 65_520:
            raise ValueError("Git stateless request packet is too large")
        payload = _read_exact(stream, length - 4)
        output.extend(payload)
        if len(output) > MAX_STATELESS_REQUEST_BYTES:
            raise ValueError("Git stateless request is too large")
        packets += 1
        if packets > MAX_STATELESS_PACKETS:
            raise ValueError("Git stateless request has too many packets")


def _connect_response(chunks: Iterable[bytes]) -> Iterable[bytes]:
    tail = b""
    for chunk in chunks:
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("Git upload-pack response is invalid")
        combined = tail + chunk
        if len(combined) > 4:
            yield combined[:-4]
            tail = combined[-4:]
        else:
            tail = combined
    if tail != b"0002":
        raise ValueError("Git upload-pack response is invalid")
    yield b"0000"


@dataclass
class StatelessRemoteHelper:
    repository: Repository
    transport: UploadPackTransport
    stdin: BinaryIO
    stdout: BinaryIO
    connect_mode: bool = False

    def run(self) -> int:
        advertisement = self.transport.discover()
        if not advertisement or len(advertisement) > 1_048_576:
            raise ValueError("Git upload-pack advertisement is invalid")
        self.stdout.write(advertisement)
        self.stdout.flush()

        while True:
            request = read_stateless_request(self.stdin)
            if request is None:
                return 0
            chunks = self.transport.rpc(request)
            if self.connect_mode:
                chunks = _connect_response(chunks)
            for chunk in chunks:
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("Git upload-pack response is invalid")
                self.stdout.write(chunk)
            self.stdout.flush()


@dataclass
class ReceivePackRemoteHelper:
    repository: Repository
    transport: ReceivePackTransport
    stdin: BinaryIO
    stdout: BinaryIO

    def run(self) -> int:
        advertisement = self.transport.discover()
        if not advertisement or len(advertisement) > 4 * 1024 * 1024:
            raise ValueError("Git receive-pack advertisement is invalid")
        self.stdout.write(advertisement)
        self.stdout.flush()
        request = self.stdin.read(MAX_RECEIVE_PACK_REQUEST_BYTES + 1)
        if not request or len(request) > MAX_RECEIVE_PACK_REQUEST_BYTES:
            raise ValueError("Git receive-pack request is invalid")
        for chunk in self.transport.push(request):
            if not isinstance(chunk, bytes) or not chunk:
                raise ValueError("Git receive-pack response is invalid")
            self.stdout.write(chunk)
        self.stdout.flush()
        return 0


def run_remote_helper(
    arguments: list[str],
    environment: Mapping[str, str],
    transport: UploadPackTransport | ReceivePackTransport,
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> int:
    if len(arguments) != 2:
        raise ValueError("Git remote helper arguments are invalid")
    expected = environment.get("AGENT_BROKER_REPOSITORY", "")
    repository = parse_broker_repository_url(arguments[1], expected)
    command = stdin.readline(4097)
    if command != b"capabilities\n":
        raise ValueError("Git remote helper command is not allowed")
    stdout.write(b"connect\nstateless-connect\n\n")
    stdout.flush()
    connect = stdin.readline(4097)
    if connect in {
        b"connect git-upload-pack\n",
        b"stateless-connect git-upload-pack\n",
    }:
        selected = (
            transport.for_service("git-upload-pack")  # type: ignore[union-attr]
            if hasattr(transport, "for_service")
            else transport
        )
        stdout.write(b"\n")
        stdout.flush()
        return StatelessRemoteHelper(
            repository,
            selected,  # type: ignore[arg-type]
            stdin,
            stdout,
            connect_mode=connect == b"connect git-upload-pack\n",
        ).run()
    if connect == b"connect git-receive-pack\n":
        selected = (
            transport.for_service("git-receive-pack")  # type: ignore[union-attr]
            if hasattr(transport, "for_service")
            else transport
        )
        stdout.write(b"\n")
        stdout.flush()
        return ReceivePackRemoteHelper(
            repository, selected, stdin, stdout  # type: ignore[arg-type]
        ).run()
    raise ValueError("Git remote helper service is not allowed")
