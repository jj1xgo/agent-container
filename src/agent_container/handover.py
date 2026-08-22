from datetime import datetime
from pathlib import Path
import re


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
HANDOVER_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}\.md$")


def validate_project_id(project_id: str) -> str:
    if project_id in {".", ".."} or PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id must be a single safe repository-style slug")
    return project_id


def _clean_line(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"{field} must be one non-empty line")
    return cleaned


def latest_handover(root: Path, project_id: str) -> Path | None:
    project = root.resolve() / validate_project_id(project_id)
    if not project.is_dir():
        return None
    candidates = [
        path
        for path in project.iterdir()
        if HANDOVER_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    ]
    return max(candidates, key=lambda path: path.name, default=None)


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
    timestamp = now or datetime.now().astimezone()
    project = root.resolve() / project_id
    project.mkdir(parents=True, exist_ok=True)
    path = project / timestamp.strftime("%Y-%m-%d_%H%M.md")
    created = timestamp.isoformat(timespec="minutes")
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
    with path.open("x", encoding="utf-8") as stream:
        stream.write(body)
    return path
