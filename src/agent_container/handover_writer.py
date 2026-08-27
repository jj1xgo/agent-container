from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets

from agent_container.handover_broker_protocol import MAX_DOCUMENT_BYTES
from agent_container.state import validate_project_id


_REQUIRED_HEADINGS = (
    "## 作業の目的",
    "## 現在地",
    "## 決定事項と理由",
    "## 変更したファイル・commit・PR",
    "## 検証結果",
    "## 未解決事項とリスク",
    "## 次の一手",
)
_CREDENTIALS = re.compile(
    r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
def validate_handover_content(title: str, body: str) -> tuple[str, str]:
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("handover title and body must be strings")
    if "\n" in title or "\r" in title:
        raise ValueError("handover title must be one non-empty line")
    title = title.strip()
    if not title:
        raise ValueError("handover title must be one non-empty line")
    if "\x00" in title or "\x00" in body:
        raise ValueError("handover content must not contain NUL")
    try:
        body_bytes = body.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("handover content must be UTF-8") from None
    if len(body_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError("handover document is too large")
    if _CREDENTIALS.search(title) or _CREDENTIALS.search(body):
        raise ValueError("handover content appears to contain credentials")

    body = body.rstrip("\n") + "\n"
    headings = tuple(line for line in body.splitlines() if line.startswith("## "))
    if not body.startswith(_REQUIRED_HEADINGS[0] + "\n") or headings != _REQUIRED_HEADINGS:
        raise ValueError("handover body must contain exactly the required sections")
    return title, body


def render_handover(project_id: str, title: str, body: str, now: datetime) -> bytes:
    project_id = validate_project_id(project_id)
    title, body = validate_handover_content(title, body)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("handover timestamp must include a timezone")
    created = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    document = (
        f"# Handover: {title}\n\n"
        f"- Project: {project_id}\n"
        f"- Created: {created}\n"
        "- Session: （未記録）\n\n"
        f"{body}"
    ).encode("utf-8")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise ValueError("handover document is too large")
    return document


def _resolve_project_directory(project_dir: Path, project_id: str) -> Path:
    project_id = validate_project_id(project_id)
    if project_dir.name != project_id or project_dir.is_symlink() or not project_dir.is_dir():
        raise ValueError("handover project directory is invalid")
    resolved = project_dir.resolve(strict=True)
    if resolved.name != project_id or resolved.is_symlink() or not resolved.is_dir():
        raise ValueError("handover project directory is invalid")
    return resolved


def _write_all(fd: int, document: bytes) -> None:
    offset = 0
    while offset < len(document):
        written = os.write(fd, document[offset:])
        if written <= 0:
            raise OSError("failed to write handover document")
        offset += written


def create_atomic_handover(
    project_dir: Path,
    project_id: str,
    title: str,
    body: str,
    now: datetime | None = None,
    token_hex: Callable[[int], str] = secrets.token_hex,
) -> Path:
    project = _resolve_project_directory(project_dir, project_id)
    timestamp = now or datetime.now(timezone.utc)
    document = render_handover(project_id, title, body, timestamp)
    timestamp = timestamp.astimezone(timezone.utc)

    for _ in range(8):
        temporary = project / f".handover-{token_hex(8)}.tmp"
        final = project / (
            timestamp.strftime("%Y-%m-%d_%H%M%S_") + token_hex(4) + ".md"
        )
        fd: int | None = None
        published = False
        complete = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
            )
            _write_all(fd, document)
            os.fsync(fd)
            os.close(fd)
            fd = None
            try:
                os.link(temporary, final)
            except FileExistsError:
                continue
            published = True
            os.unlink(temporary)
            directory_fd = os.open(project, os.O_RDONLY | _NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            complete = True
            return final
        finally:
            if fd is not None:
                os.close(fd)
            if temporary.exists() or temporary.is_symlink():
                os.unlink(temporary)
            if published and not complete and final.exists():
                os.unlink(final)
    raise FileExistsError("could not allocate unique handover path")
