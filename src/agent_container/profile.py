from pathlib import Path
import re
import shutil
import tomllib


PROFILE_VERSION = "5\n"
HANDOVER_APPROVAL_RULE = (
    'prefix_rule(pattern=["agent-handover", "create"], decision="allow")\n'
)
SANDBOX_NETWORK_TABLE = "sandbox_workspace_write"
SANDBOX_NETWORK_KEY = "network_access"
_SANDBOX_NETWORK_LINE = f"{SANDBOX_NETWORK_KEY} = true\n"
_SANDBOX_NETWORK_BLOCK = f"[{SANDBOX_NETWORK_TABLE}]\n{_SANDBOX_NETWORK_LINE}"
_TABLE_HEADER = re.compile(r"\s*\[")
_SANDBOX_NETWORK_ASSIGNMENT = re.compile(rf"\s*{SANDBOX_NETWORK_KEY}\s*=")


def _require_managed_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"managed profile path must not be a symlink: {path}")
    if not path.is_dir():
        raise FileNotFoundError(path)


def _require_managed_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"managed profile path must not be a symlink: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)


def validate_codex_handover_profile(codex_home: Path) -> None:
    _require_managed_directory(codex_home)
    version_file = codex_home / "managed-profile.version"
    _require_managed_file(version_file)
    try:
        current = version_file.read_text(encoding="utf-8")
    except UnicodeError:
        current = ""
    if current != PROFILE_VERSION:
        raise ValueError(
            "managed Codex handover profile is out of date; run "
            "agentctl project update-profile PROJECT"
        )


def seed_codex_home(profile_root: Path, codex_home: Path) -> None:
    sources = (
        (profile_root / "config.toml", codex_home / "config.toml"),
        (profile_root / "hooks.json", codex_home / "hooks.json"),
        (profile_root / "rules", codex_home / "rules"),
        (profile_root / "skills", codex_home / "skills"),
    )
    if any(target.exists() or target.is_symlink() for _, target in sources):
        existing = next(
            target for _, target in sources if target.exists() or target.is_symlink()
        )
        raise FileExistsError(f"managed profile target already exists: {existing}")
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(sources[0][0], sources[0][1])
    shutil.copy2(sources[1][0], sources[1][1])
    shutil.copytree(sources[2][0], sources[2][1], symlinks=False)
    shutil.copytree(sources[3][0], sources[3][1], symlinks=False)
    (codex_home / "managed-profile.version").write_text(PROFILE_VERSION, encoding="utf-8")


def ensure_codex_sandbox_network(config_file: Path) -> None:
    """Pin `sandbox_workspace_write.network_access = true` without touching other keys."""

    text = config_file.read_text(encoding="utf-8")
    table = tomllib.loads(text).get(SANDBOX_NETWORK_TABLE)
    if isinstance(table, dict) and table.get(SANDBOX_NETWORK_KEY) is True:
        return
    if table is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        updated = f"{text}{separator}\n{_SANDBOX_NETWORK_BLOCK}"
    else:
        lines = text.splitlines(keepends=True)
        header = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == f"[{SANDBOX_NETWORK_TABLE}]"
            ),
            None,
        )
        if header is None:
            raise ValueError(
                f"managed profile cannot update {SANDBOX_NETWORK_TABLE} in {config_file}"
            )
        end = header + 1
        while end < len(lines) and not _TABLE_HEADER.match(lines[end]):
            end += 1
        body = [
            line
            for line in lines[header + 1 : end]
            if not _SANDBOX_NETWORK_ASSIGNMENT.match(line)
        ]
        lines[header + 1 : end] = [_SANDBOX_NETWORK_LINE, *body]
        updated = "".join(lines)
    if tomllib.loads(updated)[SANDBOX_NETWORK_TABLE][SANDBOX_NETWORK_KEY] is not True:
        raise ValueError(f"managed profile update failed for {config_file}")
    config_file.write_text(updated, encoding="utf-8")


def update_codex_handover_profile(profile_root: Path, codex_home: Path) -> None:
    config_file = codex_home / "config.toml"
    rules_dir = codex_home / "rules"
    rules_file = rules_dir / "default.rules"
    skill_file = codex_home / "skills/handover/SKILL.md"
    version_file = codex_home / "managed-profile.version"
    _require_managed_directory(codex_home)
    for directory in (codex_home / "skills", codex_home / "skills/handover"):
        _require_managed_directory(directory)
    for path in (config_file, skill_file, version_file):
        _require_managed_file(path)
    if rules_dir.is_symlink():
        raise ValueError(f"managed profile path must not be a symlink: {rules_dir}")
    rules_missing = not rules_dir.exists()
    if not rules_missing:
        _require_managed_directory(rules_dir)
        _require_managed_file(rules_file)

    if rules_missing:
        # Managed profile version 1 predates the approval rules directory.
        shutil.copytree(profile_root / "rules", rules_dir, symlinks=False)

    rules = rules_file.read_text(encoding="utf-8")
    if HANDOVER_APPROVAL_RULE not in rules.splitlines(keepends=True):
        separator = "" if not rules or rules.endswith("\n") else "\n"
        rules_file.write_text(
            rules + separator + HANDOVER_APPROVAL_RULE,
            encoding="utf-8",
        )
    shutil.copy2(profile_root / "skills/handover/SKILL.md", skill_file)
    ensure_codex_sandbox_network(config_file)
    version_file.write_text(PROFILE_VERSION, encoding="utf-8")
