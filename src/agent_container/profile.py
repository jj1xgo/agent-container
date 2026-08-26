from pathlib import Path
import shutil


PROFILE_VERSION = "3\n"
HANDOVER_APPROVAL_RULE = (
    'prefix_rule(pattern=["agent-handover", "create"], decision="allow")\n'
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


def update_codex_handover_profile(profile_root: Path, codex_home: Path) -> None:
    rules_file = codex_home / "rules/default.rules"
    skill_file = codex_home / "skills/handover/SKILL.md"
    version_file = codex_home / "managed-profile.version"
    for path in (rules_file, skill_file, version_file):
        if path.is_symlink():
            raise ValueError(f"managed profile path must not be a symlink: {path}")
        if not path.is_file():
            raise FileNotFoundError(path)

    rules = rules_file.read_text(encoding="utf-8")
    if HANDOVER_APPROVAL_RULE not in rules.splitlines(keepends=True):
        separator = "" if not rules or rules.endswith("\n") else "\n"
        rules_file.write_text(
            rules + separator + HANDOVER_APPROVAL_RULE,
            encoding="utf-8",
        )
    shutil.copy2(profile_root / "skills/handover/SKILL.md", skill_file)
    version_file.write_text(PROFILE_VERSION, encoding="utf-8")
