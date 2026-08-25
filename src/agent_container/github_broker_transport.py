from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from typing import BinaryIO, Iterable

from agent_container.git_remote_helper import MAX_STATELESS_REQUEST_BYTES
from agent_container.git_remote_helper import MAX_RECEIVE_PACK_REQUEST_BYTES
from agent_container.git_protocol import gate_receive_pack_commands
from agent_container.git_protocol import parse_receive_pack_advertisement
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import MAX_STREAM_CHUNK_BYTES
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import iter_chunk_stream
from agent_container.github_broker_protocol import read_request_frame
from agent_container.github_broker_protocol import read_response_frame
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_git_transport import MAX_DISCOVERY_BYTES
from agent_container.github_git_transport import MAX_UPLOAD_PACK_RESPONSE_BYTES
from agent_container.github_git_transport import GitHubUploadPackTransport
from agent_container.github_git_transport import GitHubReceivePackTransport
from agent_container.github_git_transport import MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES
from agent_container.github_git_transport import MAX_RECEIVE_PACK_RESPONSE_BYTES


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _validate_exact_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("broker runtime path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("broker runtime path is invalid") from None
    if resolved != path:
        raise ValueError("broker runtime path must not contain symlinks")
    return resolved


def read_broker_capability(path: Path) -> str:
    _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_size > 45
    ):
        raise ValueError("broker capability file is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        body = os.read(descriptor, 46)
    finally:
        os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError("broker capability file is invalid") from None
    if _CAPABILITY.fullmatch(capability) is None or body != (capability + "\n").encode():
        raise ValueError("broker capability file is invalid")
    return capability


def validate_broker_socket(path: Path) -> Path:
    _validate_exact_path(path)
    metadata = path.stat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("broker socket is invalid")
    return path


@dataclass
class BrokerUploadPackClient:
    socket_path: Path
    capability_path: Path
    project_id: str
    repository: str
    socket_factory: object = socket.socket
    _socket: socket.socket | None = field(default=None, init=False, repr=False)
    _stream: BinaryIO | None = field(default=None, init=False, repr=False)
    _discovery: bytes | None = field(default=None, init=False, repr=False)

    def _connect(self) -> None:
        if self._stream is not None:
            return
        validate_broker_socket(self.socket_path)
        capability = read_broker_capability(self.capability_path)
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
        stream: BinaryIO | None = None
        try:
            client.settimeout(60)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            request = BrokerRequest(
                version=PROTOCOL_VERSION,
                capability=capability,
                project_id=self.project_id,
                sequence=secrets.randbelow((1 << 63) - 1) + 1,
                operation="git-upload-pack",
                payload={"repository": self.repository},
            )
            stream.write(encode_request_frame(request))
            stream.flush()
            response = read_response_frame(stream)
            if response.status != "ok":
                raise RuntimeError("Git broker request was denied")
            discovery = b"".join(
                iter_chunk_stream(stream, maximum_total=MAX_DISCOVERY_BYTES)
            )
            if not discovery:
                raise RuntimeError("Git broker discovery failed")
        except Exception:
            if stream is not None:
                stream.close()
            client.close()
            raise
        self._socket = client
        self._stream = stream
        self._discovery = discovery

    def discover(self) -> bytes:
        self._connect()
        assert self._discovery is not None
        return self._discovery

    def rpc(self, request: bytes) -> Iterable[bytes]:
        self._connect()
        if self._stream is None:
            raise RuntimeError("Git broker connection failed")
        if not isinstance(request, bytes) or not request:
            raise ValueError("Git broker request is invalid")
        chunks = (
            request[offset : offset + MAX_STREAM_CHUNK_BYTES]
            for offset in range(0, len(request), MAX_STREAM_CHUNK_BYTES)
        )
        write_chunk_stream(self._stream, chunks)
        yield from iter_chunk_stream(
            self._stream, maximum_total=MAX_UPLOAD_PACK_RESPONSE_BYTES
        )

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._discovery = None


@dataclass
class BrokerReceivePackClient(BrokerUploadPackClient):
    def _connect(self) -> None:
        if self._stream is not None:
            return
        validate_broker_socket(self.socket_path)
        capability = read_broker_capability(self.capability_path)
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
        stream: BinaryIO | None = None
        try:
            client.settimeout(60)
            client.connect(str(self.socket_path))
            stream = client.makefile("rwb", buffering=0)
            request = BrokerRequest(
                version=PROTOCOL_VERSION,
                capability=capability,
                project_id=self.project_id,
                sequence=secrets.randbelow((1 << 63) - 1) + 1,
                operation="git-receive-pack",
                payload={"repository": self.repository},
            )
            stream.write(encode_request_frame(request))
            stream.flush()
            response = read_response_frame(stream)
            if response.status != "ok":
                raise RuntimeError("Git broker request was denied")
            discovery = b"".join(
                iter_chunk_stream(
                    stream, maximum_total=MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES
                )
            )
            if not discovery:
                raise RuntimeError("Git broker discovery failed")
        except Exception:
            if stream is not None:
                stream.close()
            client.close()
            raise
        self._socket = client
        self._stream = stream
        self._discovery = discovery

    def push(self, request: bytes) -> Iterable[bytes]:
        self._connect()
        if self._stream is None:
            raise RuntimeError("Git broker connection failed")
        if not isinstance(request, bytes) or not request:
            raise ValueError("Git broker request is invalid")
        chunks = (
            request[offset : offset + MAX_STREAM_CHUNK_BYTES]
            for offset in range(0, len(request), MAX_STREAM_CHUNK_BYTES)
        )
        write_chunk_stream(self._stream, chunks)
        yield from iter_chunk_stream(
            self._stream, maximum_total=MAX_RECEIVE_PACK_RESPONSE_BYTES
        )


def handle_upload_pack_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubUploadPackTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    try:
        request = initial_request or read_request_frame(connection)
        authorized = session.authorize(request)
        if authorized["operation"] != "git-upload-pack" or authorized["payload"] != {
            "repository": session.policy.repository.slug
        }:
            raise ValueError("broker upload-pack request is not allowed")
    except (ValueError, OSError):
        connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "denied")))
        connection.flush()
        return 1

    connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "ok")))
    discovery = transport.discover()
    transferred = write_chunk_stream(connection, (discovery,))
    while True:
        try:
            chunks = iter_chunk_stream(
                connection,
                maximum_total=MAX_STATELESS_REQUEST_BYTES,
                allow_initial_eof=True,
            )
            request_body = b"".join(chunks)
        except ValueError:
            session.audit(operation="git-upload-pack", status="denied")
            return 1
        if not request_body:
            session.audit(
                operation="git-upload-pack",
                status="ok",
                bytes_transferred=transferred,
            )
            return 0
        transferred += write_chunk_stream(connection, transport.rpc(request_body))


def handle_receive_pack_connection(
    session: BrokerSession,
    connection: BinaryIO,
    transport: GitHubReceivePackTransport,
    initial_request: BrokerRequest | None = None,
) -> int:
    try:
        request = initial_request or read_request_frame(connection)
        authorized = session.authorize(request)
        if authorized["operation"] != "git-receive-pack" or authorized["payload"] != {
            "repository": session.policy.repository.slug
        }:
            raise ValueError("broker receive-pack request is not allowed")
        discovery = transport.discover()
        advertisement = parse_receive_pack_advertisement(discovery)
    except (ValueError, RuntimeError, OSError):
        connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "denied")))
        connection.flush()
        return 1

    connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "ok")))
    write_chunk_stream(connection, (discovery,))
    try:
        request_body = b"".join(
            iter_chunk_stream(
                connection, maximum_total=MAX_RECEIVE_PACK_REQUEST_BYTES
            )
        )
        gated = gate_receive_pack_commands(
            request_body, advertisement, session.policy
        )
        transferred = write_chunk_stream(connection, transport.rpc(request_body))
    except (ValueError, RuntimeError, OSError):
        session.audit(operation="git-receive-pack", status="denied")
        return 1
    for update in gated.updates:
        session.audit(
            operation="git-receive-pack",
            status="ok",
            ref=update.ref,
            bytes_transferred=transferred,
        )
    return 0


def handle_broker_connection(
    session: BrokerSession,
    connection: BinaryIO,
    upload_transport: GitHubUploadPackTransport,
    receive_transport: GitHubReceivePackTransport | None,
) -> int:
    try:
        request = read_request_frame(connection)
    except (ValueError, OSError):
        connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "denied")))
        connection.flush()
        return 1
    if request.operation == "git-upload-pack":
        return handle_upload_pack_connection(
            session, connection, upload_transport, request
        )
    if request.operation == "git-receive-pack" and receive_transport is not None:
        return handle_receive_pack_connection(
            session, connection, receive_transport, request
        )
    connection.write(encode_response_frame(BrokerResponse(PROTOCOL_VERSION, "denied")))
    connection.flush()
    return 1
