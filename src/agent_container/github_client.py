import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
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
from agent_container.github_broker_policy import MAX_ISSUE_NUMBER
from agent_container.github_broker_policy import MAX_PR_NUMBER
from agent_container.github_issue import MAX_ISSUE_RESPONSE_BYTES


_ISSUE_SUMMARY_FIELDS = frozenset(
    {
        "number",
        "title",
        "state",
        "author",
        "labels",
        "created_at",
        "updated_at",
        "url",
    }
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_ISSUE_URL = re.compile(
    r"^https://github\.com/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/issues/([1-9][0-9]*)$"
)


def _issue_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("issue number is invalid") from None
    if not 1 <= number <= MAX_ISSUE_NUMBER:
        raise argparse.ArgumentTypeError("issue number is invalid")
    return number


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
    issue = commands.add_parser("issue")
    issue_operations = issue.add_subparsers(dest="operation", required=True)
    issue_operations.add_parser("list")
    issue_view = issue_operations.add_parser("view")
    issue_view.add_argument("number", type=_issue_number)
    return parser


def _request_for(options: argparse.Namespace) -> tuple[str, dict[str, object]]:
    if options.resource == "issue":
        if options.operation == "list":
            return "issue-list", {}
        return "issue-view", {"number": options.number}
    if options.operation == "create":
        return "pr-create", {
            "base": options.base,
            "head": options.head,
            "title": options.title,
            "body": options.body,
        }
    return f"pr-{options.operation}", {"number": options.number}


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GitHub broker response is invalid")
        result[key] = value
    return result


def _text(
    value: object,
    *,
    maximum_bytes: int,
    allow_empty: bool,
    allow_newline: bool,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError("GitHub broker response is invalid")
    try:
        if len(value.encode("utf-8")) > maximum_bytes:
            raise ValueError("GitHub broker response is invalid")
    except UnicodeEncodeError:
        raise ValueError("GitHub broker response is invalid") from None
    for character in value:
        codepoint = ord(character)
        if codepoint == 127 or (
            codepoint < 32
            and not (allow_newline and character in {"\n", "\t"})
        ):
            raise ValueError("GitHub broker response is invalid")
    return value


def _number(value: object, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError("GitHub broker response is invalid")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("GitHub broker response is invalid")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("GitHub broker response is invalid") from None
    return value


def _validate_issue_summary(result: object) -> None:
    if not isinstance(result, dict) or set(result) != _ISSUE_SUMMARY_FIELDS:
        raise ValueError("GitHub broker response is invalid")
    number = _number(result["number"], MAX_ISSUE_NUMBER)
    _text(
        result["title"],
        maximum_bytes=256,
        allow_empty=False,
        allow_newline=False,
    )
    if not isinstance(result["state"], str) or result["state"] not in {
        "open",
        "closed",
    }:
        raise ValueError("GitHub broker response is invalid")
    author = result["author"]
    if author is not None:
        _text(
            author,
            maximum_bytes=MAX_ISSUE_RESPONSE_BYTES,
            allow_empty=False,
            allow_newline=False,
        )
    labels = result["labels"]
    if not isinstance(labels, list) or len(labels) > 100:
        raise ValueError("GitHub broker response is invalid")
    for label in labels:
        _text(
            label,
            maximum_bytes=100,
            allow_empty=False,
            allow_newline=False,
        )
    _timestamp(result["created_at"])
    _timestamp(result["updated_at"])
    url = result["url"]
    match = _ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or int(match.group(1)) != number:
        raise ValueError("GitHub broker response is invalid")


def _validate_pull_request_summary(result: object) -> None:
    if not isinstance(result, dict) or set(result) != {
        "number",
        "state",
        "title",
        "url",
    }:
        raise ValueError("GitHub broker response is invalid")
    _number(result["number"], MAX_PR_NUMBER)
    if not isinstance(result["state"], str):
        raise ValueError("GitHub broker response is invalid")
    _text(
        result["title"],
        maximum_bytes=256,
        allow_empty=False,
        allow_newline=False,
    )
    url = result["url"]
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ValueError("GitHub broker response is invalid")


def _validate_response(
    operation: str, payload: dict[str, object], result: object
) -> dict[str, Any]:
    if operation == "issue-list":
        if not isinstance(result, dict) or set(result) != {"issues"}:
            raise ValueError("GitHub broker response is invalid")
        issues = result["issues"]
        if not isinstance(issues, list) or len(issues) > 30:
            raise ValueError("GitHub broker response is invalid")
        for issue in issues:
            _validate_issue_summary(issue)
            if issue["state"] != "open":
                raise ValueError("GitHub broker response is invalid")
    elif operation == "issue-view":
        if not isinstance(result, dict) or set(result) != _ISSUE_SUMMARY_FIELDS | {
            "body"
        }:
            raise ValueError("GitHub broker response is invalid")
        summary = {key: value for key, value in result.items() if key != "body"}
        _validate_issue_summary(summary)
        _text(
            result["body"],
            maximum_bytes=256 * 1024,
            allow_empty=True,
            allow_newline=True,
        )
        if result["number"] != payload.get("number"):
            raise ValueError("GitHub broker response is invalid")
    elif operation in {"pr-create", "pr-view"}:
        _validate_pull_request_summary(result)
        if operation == "pr-view" and result["number"] != payload.get("number"):
            raise ValueError("GitHub broker response is invalid")
    elif operation == "pr-checks":
        if not isinstance(result, dict) or set(result) != {"number", "checks"}:
            raise ValueError("GitHub broker response is invalid")
        number = _number(result["number"], MAX_PR_NUMBER)
        checks = result["checks"]
        if (
            number != payload.get("number")
            or not isinstance(checks, list)
            or len(checks) > 100
        ):
            raise ValueError("GitHub broker response is invalid")
        for check in checks:
            if not isinstance(check, dict) or set(check) != {
                "name",
                "status",
                "conclusion",
            }:
                raise ValueError("GitHub broker response is invalid")
            if not isinstance(check["name"], str) or not isinstance(
                check["status"], str
            ):
                raise ValueError("GitHub broker response is invalid")
            if check["conclusion"] is not None and not isinstance(
                check["conclusion"], str
            ):
                raise ValueError("GitHub broker response is invalid")
    else:
        raise ValueError("GitHub broker response is invalid")
    if not isinstance(result, dict):
        raise ValueError("GitHub broker response is invalid")
    return result


def request_github_operation(
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
        body = b"".join(
            iter_chunk_stream(stream, maximum_total=MAX_ISSUE_RESPONSE_BYTES)
        )
        decoded = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("GitHub broker response is invalid")
            ),
        )
        return _validate_response(operation, payload, decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
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
    requester=request_github_operation,  # type: ignore[no-untyped-def]
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
