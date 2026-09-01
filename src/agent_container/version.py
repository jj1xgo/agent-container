from collections.abc import Mapping
import os
from pathlib import Path
import re
import subprocess

from .release_metadata import (
    DEVELOPMENT_BASE_TAG,
    DEVELOPMENT_VERSION,
    FALLBACK_VERSION,
    RELEASE_TAG,
    RELEASE_VERSION,
)

_NUMERIC_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = (
    rf"(?:{_NUMERIC_IDENTIFIER}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_SEMVER = re.compile(
    rf"{_NUMERIC_IDENTIFIER}\."
    rf"{_NUMERIC_IDENTIFIER}\."
    rf"{_NUMERIC_IDENTIFIER}"
    rf"(?:-{_PRERELEASE_IDENTIFIER}(?:\.{_PRERELEASE_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def _git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository_root), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


def resolve_version(
    repository_root: Path,
    environment: Mapping[str, str] | None = None,
) -> str:
    configured = (os.environ if environment is None else environment).get(
        "AGENT_CONTAINER_VERSION"
    )
    try:
        if _git(repository_root, "rev-parse", "--is-inside-work-tree") != "true":
            raise ValueError("not a Git worktree")
        commit = _git(repository_root, "rev-parse", "--short=7", "HEAD")
        dirty = _git(
            repository_root,
            "status",
            "--porcelain",
            "--untracked-files=no",
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        if configured is not None and _SEMVER.fullmatch(configured) is not None:
            return configured
        return FALLBACK_VERSION

    try:
        exact_tag = _git(
            repository_root,
            "describe",
            "--tags",
            "--exact-match",
            "--match",
            RELEASE_TAG,
        )
    except (OSError, subprocess.SubprocessError):
        exact_tag = ""
    if not dirty and exact_tag == RELEASE_TAG:
        return RELEASE_VERSION

    try:
        _git(
            repository_root,
            "merge-base",
            "--is-ancestor",
            DEVELOPMENT_BASE_TAG,
            "HEAD",
        )
        distance = _git(
            repository_root,
            "rev-list",
            "--count",
            "--first-parent",
            f"{DEVELOPMENT_BASE_TAG}..HEAD",
        )
    except (OSError, subprocess.SubprocessError):
        distance = "0"

    suffix = ".dirty" if dirty else ""
    return f"{DEVELOPMENT_VERSION}.{distance}+g{commit}{suffix}"
