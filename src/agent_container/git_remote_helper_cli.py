import os
from pathlib import Path
import sys

from agent_container.git_remote_helper import run_remote_helper
from agent_container.github_broker_transport import BrokerUploadPackClient


def main() -> int:
    client = BrokerUploadPackClient(
        socket_path=Path(os.environ.get("AGENT_BROKER_SOCKET", "")),
        capability_path=Path(os.environ.get("AGENT_BROKER_CAPABILITY", "")),
        project_id=os.environ.get("AGENT_PROJECT_ID", ""),
        repository=os.environ.get("AGENT_BROKER_REPOSITORY", ""),
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
