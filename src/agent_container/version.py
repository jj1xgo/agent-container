from collections.abc import Mapping
import os
from pathlib import Path
import re
import subprocess


_FALLBACK_VERSION = "0.3.0-dev.0"
_RELEASE_TAG = "v0.2.0"
_RELEASE_VERSION = _RELEASE_TAG.removeprefix("v")
_DEVELOPMENT_VERSION = "0.3.0-dev"
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
        return _FALLBACK_VERSION

    try:
        exact_tag = _git(
            repository_root,
            "describe",
            "--tags",
            "--exact-match",
            "--match",
            _RELEASE_TAG,
        )
    except (OSError, subprocess.SubprocessError):
        exact_tag = ""
    if not dirty and exact_tag == _RELEASE_TAG:
        return _RELEASE_VERSION

    try:
        _git(repository_root, "merge-base", "--is-ancestor", _RELEASE_TAG, "HEAD")
        distance = _git(
            repository_root,
            "rev-list",
            "--count",
            "--first-parent",
            f"{_RELEASE_TAG}..HEAD",
        )
    except (OSError, subprocess.SubprocessError):
        distance = "0"

    suffix = ".dirty" if dirty else ""
    return f"{_DEVELOPMENT_VERSION}.{distance}+g{commit}{suffix}"
