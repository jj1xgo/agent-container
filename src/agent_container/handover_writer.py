from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import stat

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
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def validate_handover_content(title: str, body: str) -> tuple[str, str]:
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("handover title and body must be strings")
    if "\n" in title or "\r" in title:
        raise ValueError("handover title must be one non-empty line")
    title = title.strip()
    if not title:
        raise ValueError("handover title must be one non-empty line")
    body = body.rstrip("\n") + "\n"
    try:
        title.encode("utf-8")
        body_bytes = body.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("handover content must be UTF-8") from None
    if "\x00" in title or "\x00" in body:
        raise ValueError("handover content must not contain NUL")
    if len(body_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError("handover document is too large")
    if _CREDENTIALS.search(title) or _CREDENTIALS.search(body):
        raise ValueError("handover content appears to contain credentials")

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


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _revalidate_project_directory(
    project_dir: Path, directory_fd: int, expected: os.stat_result
) -> None:
    try:
        current = os.lstat(project_dir)
        pinned = os.fstat(directory_fd)
    except OSError:
        raise ValueError("handover project directory changed") from None
    if (
        not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(pinned.st_mode)
        or not _same_inode(current, expected)
        or not _same_inode(pinned, expected)
    ):
        raise ValueError("handover project directory changed")


def _open_project_directory(project_dir: Path, project_id: str) -> tuple[Path, int]:
    project_id = validate_project_id(project_id)
    if not project_dir.is_absolute() or project_dir.name != project_id:
        raise ValueError("handover project directory is invalid")
    try:
        resolved = project_dir.resolve(strict=True)
        initial = os.lstat(project_dir)
    except OSError:
        raise ValueError("handover project directory is invalid") from None
    if resolved != project_dir or not stat.S_ISDIR(initial.st_mode):
        raise ValueError("handover project directory is invalid")
    try:
        directory_fd = os.open(project_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        raise ValueError("handover project directory is invalid") from None
    try:
        _revalidate_project_directory(project_dir, directory_fd, initial)
    except BaseException:
        os.close(directory_fd)
        raise
    return project_dir, directory_fd


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
    project, directory_fd = _open_project_directory(project_dir, project_id)
    timestamp = now or datetime.now(timezone.utc)
    document = render_handover(project_id, title, body, timestamp)
    timestamp = timestamp.astimezone(timezone.utc)

    try:
        for _ in range(8):
            temporary = f".handover-{token_hex(8)}.tmp"
            final = timestamp.strftime("%Y-%m-%d_%H%M%S_") + token_hex(4) + ".md"
            fd: int | None = None
            temporary_created = False
            published = False
            complete = False
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                temporary_created = True
                os.fchmod(fd, 0o600)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise PermissionError("handover file must have mode 0600")
                _write_all(fd, document)
                os.fsync(fd)
                os.close(fd)
                fd = None
                pinned = os.fstat(directory_fd)
                _revalidate_project_directory(project, directory_fd, pinned)
                try:
                    os.link(
                        temporary,
                        final,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                published = True
                os.unlink(temporary, dir_fd=directory_fd)
                temporary_created = False
                os.fsync(directory_fd)
                _revalidate_project_directory(project, directory_fd, pinned)
                complete = True
                return project / final
            finally:
                if fd is not None:
                    os.close(fd)
                if temporary_created:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                if published and not complete:
                    try:
                        os.unlink(final, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
        raise FileExistsError("could not allocate unique handover path")
    finally:
        os.close(directory_fd)
