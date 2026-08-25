import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from typing import Any, Mapping, Sequence, TextIO

from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import iter_chunk_stream
from agent_container.github_broker_protocol import read_response_frame
from agent_container.github_broker_transport import read_broker_capability
from agent_container.github_broker_transport import validate_broker_socket
from agent_container.github_pr import MAX_PR_RESPONSE_BYTES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-github")
    commands = parser.add_subparsers(dest="resource", required=True)
    pr = commands.add_parser("pr")
    operations = pr.add_subparsers(dest="operation", required=True)
    create = operations.add_parser("create")
    create.add_argument("--base", required=True)
    create.add_argument("--head", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    for operation in ("view", "checks"):
        command = operations.add_parser(operation)
        command.add_argument("number", type=int)
    return parser


def _request_for(options: argparse.Namespace) -> tuple[str, dict[str, object]]:
    if options.operation == "create":
        return "pr-create", {
            "base": options.base,
            "head": options.head,
            "title": options.title,
            "body": options.body,
        }
    return f"pr-{options.operation}", {"number": options.number}


def request_pull_request(
    operation: str,
    payload: dict[str, object],
    environment: Mapping[str, str],
    *,
    socket_factory: object = socket.socket,
) -> dict[str, Any]:
    socket_path = Path(environment.get("AGENT_BROKER_SOCKET", ""))
    capability_path = Path(environment.get("AGENT_BROKER_CAPABILITY", ""))
    project_id = environment.get("AGENT_PROJECT_ID", "")
    validate_broker_socket(socket_path)
    capability = read_broker_capability(capability_path)
    client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[operator]
    stream = None
    try:
        client.settimeout(60)
        client.connect(str(socket_path))
        stream = client.makefile("rwb", buffering=0)
        request = BrokerRequest(
            version=PROTOCOL_VERSION,
            capability=capability,
            project_id=project_id,
            sequence=secrets.randbelow((1 << 63) - 1) + 1,
            operation=operation,
            payload=payload,
        )
        stream.write(encode_request_frame(request))
        stream.flush()
        response = read_response_frame(stream)
        if response.status != "ok":
            raise RuntimeError("GitHub broker request was denied")
        body = b"".join(iter_chunk_stream(stream, maximum_total=MAX_PR_RESPONSE_BYTES))
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("GitHub broker response is invalid")
        return decoded
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub broker response is invalid") from None
    finally:
        if stream is not None:
            stream.close()
        client.close()


def run(
    argv: Sequence[str],
    environment: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
    *,
    requester=request_pull_request,  # type: ignore[no-untyped-def]
) -> int:
    try:
        options = _parser().parse_args(argv)
        operation, payload = _request_for(options)
        result = requester(operation, payload, environment)
        json.dump(result, stdout, ensure_ascii=False, sort_keys=True)
        stdout.write("\n")
        return 0
    except (ValueError, RuntimeError, OSError):
        print("error: GitHub broker request failed", file=stderr)
        return 1


def main() -> int:
    return run(sys.argv[1:], os.environ, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
