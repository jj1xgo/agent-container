import argparse
from pathlib import Path

from agent_container.handover import create_handover


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agent-handover")
    subcommands = command.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--session-id", default="")
    return command


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "create":
        path = create_handover(
            root=arguments.root,
            project_id=arguments.project,
            title=arguments.title,
            session_id=arguments.session_id,
        )
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
