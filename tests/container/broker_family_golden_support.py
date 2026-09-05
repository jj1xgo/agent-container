"""Synthetic inputs shared with the baseline-only golden exporter."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_protocol import FamilyIntakeResponse
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import encode_response_frame
from agent_container.family_pending import append_family_audit


BASELINE = "39fbc5e997946cc915013190b78139eb09c94d48"


def example_request():
    return FamilyIntakeRequest(
        1, "issue_create_request", "A" * 43,
        {"title": "共有の確認", "summary": "Export café",
         "context": "synthetic context", "acceptance_criteria": ["条件1", "条件2"]},
    )


def example_response():
    return FamilyIntakeResponse(1, "pending", "0123456789abcdef" * 2, 1800086400)


def collect_golden():
    request = example_request()
    response = example_response()
    boundary = FamilyIntakeRequest(
        1, "issue_create_request", "c",
        {"title": "t" * 256, "summary": "s" * 2048, "context": "c" * 4096,
         "acceptance_criteria": ["a" * 512] * 19 + ["a" * 54]},
    )
    boundary_bytes = encode_request_frame(boundary)
    with TemporaryDirectory() as temp:
        path = Path(temp) / "events.jsonl"
        for timestamp, operation, status, stage in (
            (1800000000, "intake", "pending", "intake"),
            (1800000001, "intake", "denied", "validation"),
            (1800000002, "approve", "unknown", "send"),
        ):
            append_family_audit(
                path, timestamp=timestamp, project_id="demo", request_id="01" * 16,
                operation=operation, status=status, stage=stage,
            )
        audit = path.read_bytes()
    return {
        "baseline": BASELINE,
        "request": encode_request_frame(request).hex(),
        "response": encode_response_frame(response).hex(),
        "boundary_request_bytes": len(boundary_bytes),
        "boundary_request_sha256": hashlib.sha256(boundary_bytes).hexdigest(),
        "audit": audit.hex(),
    }


if __name__ == "__main__":
    print(json.dumps(collect_golden(), sort_keys=True, indent=2))
