"""Per-connection peer admission shared by every broker runtime."""

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent_container.broker.runtime import Connection


class PeerPolicy(Protocol):
    """True admits the connection; False closes it unread; raise to fail the runtime."""

    def admit(self, connection: "Connection") -> bool: ...


class SameUser:
    def admit(self, connection: "Connection") -> bool:
        return connection.peer_uid == os.getuid()
