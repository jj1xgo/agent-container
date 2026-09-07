"""One-frame Unix connection handling for family intake."""

from pathlib import Path
from typing import BinaryIO

from agent_container.broker.runtime import Connection
from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_broker import FamilyIntakeDenied
from agent_container.family_intake_broker import FamilyIntakeInternalError
from agent_container.family_intake_protocol import read_request_frame
from agent_container.family_intake_protocol import write_response_frame


class FamilyPeerPolicy:
    """Admit only the registered runtime's process tree.

    A denial closes the connection unread (kernel `admit_connection`): no
    response, no audit, no capability consumption. Any exception other than
    `FamilyIntakeDenied` propagates and fails the runtime closed.
    """

    def __init__(self, session: FamilyIntakeSession) -> None:
        self._session = session

    def admit(self, connection: Connection) -> bool:
        try:
            self._session.validate_peer(connection.peer_pid, connection.peer_uid)
        except FamilyIntakeDenied:
            return False
        return True


def handle_family_intake_connection(
    connection: Connection,
    session: FamilyIntakeSession,
    store: Path,
) -> None:
    """Close ordinary denials silently; propagate sanitized internal failures."""

    stream: BinaryIO = connection.stream
    try:
        if not session.owns_store(store):
            raise ValueError("family intake store is invalid")
        request = read_request_frame(stream)
        response = session.handle(request)
        write_response_frame(stream, response)
    except FamilyIntakeInternalError:
        raise
    except FamilyIntakeDenied:
        return
    except (OSError, TypeError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, TypeError, ValueError):
            pass
