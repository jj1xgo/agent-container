from datetime import datetime, timezone
from pathlib import Path
import re
import secrets

from agent_container.state import validate_project_id


HANDOVER_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{4}(?:\d{2}_[0-9a-f]{8})?\.md$"
)
_CREATED_LINE = re.compile(r"^- Created: (\S+)$", re.MULTILINE)
_METADATA_BYTES = 8192


def _clean_line(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"{field} must be one non-empty line")
    return cleaned


def latest_handover(root: Path, project_id: str) -> Path | None:
    project = root.resolve() / validate_project_id(project_id)
    if project.is_symlink():
        return None
    if not project.is_dir():
        return None
    candidates = [
        path
        for path in project.iterdir()
        if HANDOVER_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    ]
    return max(candidates, key=_handover_order, default=None)


def _handover_order(path: Path) -> tuple[float, int, str]:
    try:
        modified_ns = path.stat().st_mtime_ns
        with path.open("rb") as stream:
            metadata = stream.read(_METADATA_BYTES)
        text = metadata.decode("utf-8")
        match = _CREATED_LINE.search(text)
        if match is not None:
            created = datetime.fromisoformat(match.group(1))
            if created.tzinfo is not None and created.utcoffset() is not None:
                return created.timestamp(), modified_ns, path.name
    except (OSError, UnicodeDecodeError, ValueError):
        return float("-inf"), -1, path.name
    return modified_ns / 1_000_000_000, modified_ns, path.name


def create_handover(
    root: Path,
    project_id: str,
    title: str,
    session_id: str,
    now: datetime | None = None,
) -> Path:
    project_id = validate_project_id(project_id)
    title = _clean_line(title, "title")
    session = _clean_line(session_id, "session_id") if session_id.strip() else "（未記録）"
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("handover timestamp must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc)
    project = root.resolve() / project_id
    if project.is_symlink():
        raise ValueError("project directory must not be a symlink")
    project.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        path = project / (
            timestamp.strftime("%Y-%m-%d_%H%M%S_") + secrets.token_hex(4) + ".md"
        )
        try:
            stream = path.open("x", encoding="utf-8")
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("could not allocate unique handover path")
    created = timestamp.isoformat(timespec="seconds")
    body = f"""# Handover: {title}

- Project: {project_id}
- Created: {created}
- Session: {session}

## 作業の目的

## 現在地

## 決定事項と理由

## 変更したファイル・commit・PR

## 検証結果

## 未解決事項とリスク

## 次の一手
"""
    with stream:
        stream.write(body)
    return path
