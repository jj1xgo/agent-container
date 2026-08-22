from pathlib import Path
import shutil


PROFILE_VERSION = "1\n"


def seed_codex_home(profile_root: Path, codex_home: Path) -> None:
    sources = (
        (profile_root / "config.toml", codex_home / "config.toml"),
        (profile_root / "hooks.json", codex_home / "hooks.json"),
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
    (codex_home / "managed-profile.version").write_text(PROFILE_VERSION, encoding="utf-8")
