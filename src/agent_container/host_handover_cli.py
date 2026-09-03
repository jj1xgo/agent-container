import argparse
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import pwd
import sys

from agent_container.host_handover import discover_host_handover
from agent_container.host_handover import publish_host_handover


def _home_directory() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-handover-host")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--title", required=True)
    publish.add_argument("--body-file", required=True, type=Path)
    commands.add_parser("discover")
    return parser


def _session_id(env: Mapping[str, str]) -> str:
    return env.get("CODEX_SESSION_ID") or env.get("CLAUDE_SESSION_ID", "")


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
    if arguments.command == "discover":
        try:
            found = discover_host_handover(
                cwd=workspace,
                projects_root=projects_root,
            )
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            print("agent-handover-host: discovery refused", file=sys.stderr)
            return 1
        if found is not None:
            print(found)
        return 0
    try:
        created = publish_host_handover(
            cwd=workspace,
            projects_root=projects_root,
            title=arguments.title,
            body_file=arguments.body_file,
            session_id=_session_id(env),
        )
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        print("agent-handover-host: publication refused", file=sys.stderr)
        return 1
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
