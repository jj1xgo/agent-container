"""Container-only client for one credential-free family Issue request."""

import argparse
import os
from pathlib import Path
import socket
import sys
from typing import Callable, Mapping, Sequence, TextIO

from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_protocol import FamilyIntakeResponse
from agent_container.family_intake_protocol import PROTOCOL_VERSION
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import read_response_frame
from agent_container.family_intake_protocol import write_request_frame
from agent_container.family_issue import parse_family_issue_draft


_SOCKET_TIMEOUT_SECONDS = 30


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _: str) -> None:
        raise ValueError("family intake arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="agent-family", add_help=False)
    commands = parser.add_subparsers(
        dest="resource", required=True, parser_class=_ArgumentParser
    )
    issue = commands.add_parser("issue", add_help=False)
    operations = issue.add_subparsers(
        dest="operation", required=True, parser_class=_ArgumentParser
    )
    create = operations.add_parser("create", add_help=False)
    create.add_argument("--title", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--context", required=True)
    create.add_argument("--acceptance-criterion", dest="acceptance_criteria", action="append", required=True)
    return parser


def _exact_socket_path(value: object) -> Path:
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or value.endswith("/")
        or any(
            ord(character) < 32 or 0x7F <= ord(character) <= 0x9F
            for character in value
        )
    ):
        raise ValueError("family intake socket is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError("family intake socket is invalid")
    return Path(value)


def connect_family_intake(
    request: FamilyIntakeRequest,
    socket_path: Path,
    *,
    socket_factory: Callable[[int, int], socket.socket] = socket.socket,
) -> FamilyIntakeResponse:
    socket_path = _exact_socket_path(str(socket_path))
    client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    stream = None
    try:
        client.settimeout(_SOCKET_TIMEOUT_SECONDS)
        client.connect(str(socket_path))
        stream = client.makefile("rwb", buffering=0)
        write_request_frame(stream, request)
        response = read_response_frame(stream)
        try:
            trailing = stream.read(1)
        except (OSError, TypeError, ValueError):
            raise ValueError("family intake response is invalid") from None
        if trailing != b"":
            raise ValueError("family intake response is invalid")
        return response
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            client.close()


def run_create(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    connector: Callable[[FamilyIntakeRequest, Path], FamilyIntakeResponse] = connect_family_intake,
) -> FamilyIntakeResponse:
    options = _parser().parse_args(argv)
    if options.resource != "issue" or options.operation != "create":
        raise ValueError("family intake arguments are invalid")
    payload = {
        "title": options.title,
        "summary": options.summary,
        "context": options.context,
        "acceptance_criteria": options.acceptance_criteria,
    }
    parse_family_issue_draft(payload)
    socket_path = _exact_socket_path(environment.get("AGENT_FAMILY_SOCKET", ""))
    capability = environment.get("AGENT_FAMILY_CAPABILITY", "")
    request: FamilyIntakeRequest | None = None
    try:
        request = FamilyIntakeRequest(
            version=PROTOCOL_VERSION,
            operation="issue_create_request",
            capability=capability,
            payload=payload,
        )
        encode_request_frame(request)
        response = connector(request, socket_path)
        return response
    finally:
        request = None
        capability = ""


def run(
    argv: Sequence[str],
    environment: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
    *,
    connector: Callable[[FamilyIntakeRequest, Path], FamilyIntakeResponse] = connect_family_intake,
) -> int:
    try:
        response = run_create(argv, environment=environment, connector=connector)
        stdout.write(f"pending {response.request_id} {response.expires_at}\n")
        return 0
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        print("error: family intake request failed", file=stderr)
        return 1


def main() -> int:
    return run(sys.argv[1:], os.environ, sys.stdout, sys.stderr)
