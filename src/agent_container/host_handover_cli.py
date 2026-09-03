import argparse
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import pwd
import sys

from agent_container.host_handover import publish_host_handover


def _home_directory() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-handover-host")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--title", required=True)
    publish.add_argument("--body-file", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    env = os.environ if environment is None else environment
    workspace = Path.cwd() if cwd is None else cwd
    projects_root = (
        _home_directory() / ".local/share/agent-container/projects"
    )
    try:
        created = publish_host_handover(
            cwd=workspace,
            projects_root=projects_root,
            title=arguments.title,
            body_file=arguments.body_file,
            session_id=env.get("CODEX_SESSION_ID", ""),
        )
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        print("agent-handover-host: publication refused", file=sys.stderr)
        return 1
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
