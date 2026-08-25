import os
from pathlib import Path
import sys

from agent_container.git_remote_helper import run_remote_helper
from agent_container.github_broker_transport import BrokerUploadPackClient
from agent_container.github_broker_transport import BrokerReceivePackClient


class BrokerGitClient:
    def __init__(self, upload: BrokerUploadPackClient, receive: BrokerReceivePackClient):
        self.upload = upload
        self.receive = receive

    def for_service(self, service: str):  # type: ignore[no-untyped-def]
        if service == "git-upload-pack":
            return self.upload
        if service == "git-receive-pack":
            return self.receive
        raise ValueError("Git broker service is invalid")

    def close(self) -> None:
        self.upload.close()
        self.receive.close()


def main() -> int:
    options = dict(
        socket_path=Path(os.environ.get("AGENT_BROKER_SOCKET", "")),
        capability_path=Path(os.environ.get("AGENT_BROKER_CAPABILITY", "")),
        project_id=os.environ.get("AGENT_PROJECT_ID", ""),
        repository=os.environ.get("AGENT_BROKER_REPOSITORY", ""),
    )
    client = BrokerGitClient(
        BrokerUploadPackClient(**options), BrokerReceivePackClient(**options)
    )
    try:
        return run_remote_helper(
            sys.argv[1:], os.environ, client, sys.stdin.buffer, sys.stdout.buffer
        )
    except (ValueError, RuntimeError, OSError):
        print("fatal: agent Git broker failed", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
