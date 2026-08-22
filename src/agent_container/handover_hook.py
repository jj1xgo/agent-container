import json
import os
import sys
from pathlib import Path

from agent_container.handover import latest_handover


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") != "SessionStart":
        return 0
    root = os.environ.get("AGENT_HANDOVER_ROOT")
    project_id = os.environ.get("AGENT_PROJECT_ID")
    if not root or not project_id:
        return 0
    try:
        path = latest_handover(Path(root), project_id)
    except (ValueError, OSError):
        return 0
    if path is None:
        return 0
    context = (
        f"このprojectの最新handoverがあります: {path}\n"
        "別セッションの続きに必要な場合だけ本文を読み、現在のGit状態と照合してください。"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
