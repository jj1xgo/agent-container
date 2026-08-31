"""One-frame Unix connection handling for family intake."""

from pathlib import Path
import socket
import struct
from typing import BinaryIO

from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_protocol import read_request_frame
from agent_container.family_intake_protocol import write_response_frame


_PEER_CREDENTIAL_BYTES = 12


def handle_family_intake_connection(
    connection: socket.socket,
    session: FamilyIntakeSession,
    store: Path,
) -> None:
    """Handle one request, closing silently on every denied/error path."""

    stream: BinaryIO | None = None
    try:
        if not session.owns_store(store):
            raise ValueError("family intake store is invalid")
        credentials = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _PEER_CREDENTIAL_BYTES,
        )
        if type(credentials) is not bytes or len(credentials) != _PEER_CREDENTIAL_BYTES:
            raise ValueError("family intake peer credentials are invalid")
        peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        session.validate_peer(peer_pid, peer_uid)
        stream = connection.makefile("rwb", buffering=0)
        request = read_request_frame(stream)
        response = session.handle(request)
        write_response_frame(stream, response)
    except (OSError, RuntimeError, TypeError, ValueError, struct.error):
        return
    finally:
        if stream is not None:
            try:
                stream.close()
            except (OSError, TypeError, ValueError):
                pass
