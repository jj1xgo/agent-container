import io
import json
from pathlib import Path
import tempfile
from unittest import mock

from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_protocol import BrokerRequest, BrokerResponse
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import write_chunk_stream


def collect_golden() -> dict[str, object]:
    payloads = {
        "git-upload-pack": {"repository": "demo/repo"},
        "git-receive-pack": {"repository": "demo/repo"},
        "pr-create": {
            "base": "main",
            "head": "feat/demo",
            "title": "日本語",
            "body": "line\n2",
        },
        "pr-view": {"number": 7},
        "pr-checks": {"number": 7},
        "issue-list": {},
        "issue-view": {"number": 8},
    }
    requests = {
        op: encode_request_frame(
            BrokerRequest(1, "A" * 43, "demo", 123, op, payload)
        ).hex()
        for op, payload in payloads.items()
    }
    responses = {
        status: encode_response_frame(BrokerResponse(1, status)).hex()
        for status in ("ok", "denied", "error")
    }
    stream = io.BytesIO()
    transferred = write_chunk_stream(stream, (b"abc", b"\x00\xff"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        policy = BrokerPolicy.create(
            project_id="demo",
            repository="demo/repo",
            default_branch="main",
            protected_branches=("main",),
        )
        session = BrokerSession(
            policy=policy,
            run_id="0123456789abcdef",
            run_dir=root,
            socket_path=root / "broker.sock",
            capability_path=root / "capability",
            audit_file=root / "events.jsonl",
            _capability="A" * 43,
        )
        with mock.patch("agent_container.github_broker.datetime") as clock:
            clock.now.return_value.isoformat.return_value = (
                "2026-09-05T00:00:00+00:00"
            )
            for operation in payloads:
                options = {}
                if operation == "git-receive-pack":
                    options["ref"] = "refs/heads/feat/demo"
                if operation.startswith("pr-"):
                    options["pr_number"] = 7
                if operation == "issue-view":
                    options["issue_number"] = 8
                session.audit(
                    operation=operation,
                    status="ok",
                    bytes_transferred=5,
                    **options,
                )
            session.audit(operation="pr-view", status="denied", pr_number=7)
            session.audit(
                operation="issue-view",
                status="error",
                stage="issue-request",
                issue_number=8,
            )
        audit = session.audit_file.read_bytes().hex()
    return {
        "requests": requests,
        "responses": responses,
        "chunks": stream.getvalue().hex(),
        "transferred": transferred,
        "audit": audit,
    }


if __name__ == "__main__":
    print(json.dumps(collect_golden(), ensure_ascii=True, sort_keys=True, indent=2))
