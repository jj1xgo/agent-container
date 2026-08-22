# Phase 1 Codex Container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agent-container`自身を検証projectとして、専用workspace・認証・project state・handoverだけをmountしたrootless Podman内でCodexを起動できる最小実用版を作る。

**Architecture:** Python標準ライブラリの`agentctl`が入力とhost stateを検証し、秘密値を含まないPodman argvを組み立てる。固定ContainerfileはCodex、gh、Git、Pythonと管理profileを提供し、共有するのはCodex `auth.json`と専用gh認証だけ、session・config・cache・workspace・handoverはproject単位で分離する。

**Tech Stack:** Python 3.11+標準ライブラリ、`unittest`、rootless Podman 5.8+、Codex CLI 0.149.0、GitHub CLI、Git、Debian/Node container image

**Spec:** `docs/superpowers/specs/2026-08-22-phase-1-codex-container-design.md`

## Global Constraints

- 初期対象はLinuxホストとrootless Podmanだけとする。
- ホストの実`~/.codex`、`~/.claude`、既存開発workspace、Obsidian vault全体をmountしない。
- 状態rootは`${AGENT_CONTAINER_HOME:-${XDG_DATA_HOME:-~/.local/share}/agent-container}`とする。
- credential本文をargv、ログ、例外、Git、handover、test fixture、model promptへ出さない。
- CodexはChatGPT loginを使用し、`cli_auth_credentials_store = "file"`と`forced_login_method = "chatgpt"`を設定する。
- GitHub CLIは状態rootの`gh`だけを`GH_CONFIG_DIR`として使用する。
- 通常containerはrootless、`--rm`、read-only rootfs、全capability drop、no-new-privilegesで実行する。
- 自動reset、clean、force-push、`main`直接push、merge、release、repository削除を実装しない。
- network domain allowlistはこのplanのscope外であり、制限済みと表示しない。
- 新しいPython dependencyを追加しない。
- 全実装はテストを先に失敗させてから最小実装を追加する。

---

## File Structure

- `Containerfile`: Codex、gh、Git、Python、runtime source、base profileを含む固定image。
- `.containerignore`: credential、worktree metadata、cacheをbuild contextから除外する。
- `bin/agentctl`: repository checkoutからPython moduleを呼ぶ薄いentry point。
- `src/agent_container/state.py`: repository/project検証、状態root、path、permission、project metadata。
- `src/agent_container/profile.py`: base Codex profileを新規stateへseedし、既存runtime設定を上書きしない。
- `src/agent_container/podman.py`: secretを受け取らない`CommandSpec`とPodman argv builder、subprocess runner。
- `src/agent_container/agentctl.py`: `build`、`auth codex`、`project add`、`run`、`doctor`のCLI orchestration。
- `tests/container/test_state.py`: repository、path traversal、symlink、permission、metadataのtest。
- `tests/container/test_profile.py`: auth設定、managed profile seed、非上書きのtest。
- `tests/container/test_image.py`: Containerfileとbuild contextの静的contract test。
- `tests/container/test_podman.py`: build/auth/clone/runのargvとmount境界のtest。
- `tests/container/test_agentctl.py`: fake runnerを使うCLI flow、失敗時非破壊、secret非表示のtest。
- `docs/phase1-codex-container.md`: 日常command、初回認証、doctor、既知のnetwork制約。
- `docs/phase1-smoke-test.md`: 実hostで承認を挟みながら行うsmoke test手順と記録欄。

### Task 1: State boundary and repository validation

**Files:**
- Create: `src/agent_container/state.py`
- Create: `tests/container/__init__.py`
- Create: `tests/container/test_state.py`
- Modify: `src/agent_container/handover.py`
- Modify: `tests/codex/test_handover.py`

**Interfaces:**
- Consumes: `AGENT_CONTAINER_HOME`、`XDG_DATA_HOME`、project ID、`OWNER/REPOSITORY`、handover root。
- Produces: `Repository.parse(value) -> Repository`、`StateLayout.from_environment(project_id, environment=None) -> StateLayout`、`ProjectRecord`、`ensure_private_directory(path)`、`ensure_private_file(path)`、共有`validate_project_id()`。

- [ ] **Step 1: Move project ID validation to the shared state boundary and write failing tests**

Create `tests/container/__init__.py` and `tests/container/test_state.py` with the concrete cases:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import validate_project_id


class StateValidationTest(unittest.TestCase):
    def test_repository_accepts_owner_and_name(self) -> None:
        repository = Repository.parse("jj1xgo/agent-container")
        self.assertEqual(repository.owner, "jj1xgo")
        self.assertEqual(repository.name, "agent-container")
        self.assertEqual(repository.https_url, "https://github.com/jj1xgo/agent-container.git")

    def test_repository_rejects_paths_and_control_characters(self) -> None:
        for value in ("agent-container", "a/b/c", "../x", "a/..", "a/b\nnext"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Repository.parse(value)

    def test_state_layout_stays_under_explicit_root(self) -> None:
        with TemporaryDirectory() as temp:
            layout = StateLayout.from_environment(
                "agent-container", {"AGENT_CONTAINER_HOME": temp}
            )
            self.assertEqual(layout.root, Path(temp).resolve())
            self.assertEqual(layout.workspace, Path(temp).resolve() / "workspaces/agent-container")
            self.assertEqual(layout.codex_auth_file, Path(temp).resolve() / "shared-auth/codex/auth.json")

    def test_project_id_rejects_path_traversal(self) -> None:
        for value in ("", ".", "..", "../agent", "family/project"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_project_id(value)
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_container.state'`.

- [ ] **Step 3: Implement immutable repository and layout types**

Create `src/agent_container/state.py` with these exact public types and validation rules:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os
import re


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def validate_project_id(value: str) -> str:
    if value in {".", ".."} or PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id must be a single safe repository-style slug")
    return value


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @classmethod
    def parse(cls, value: str) -> "Repository":
        parts = value.split("/")
        if len(parts) != 2 or any(
            part in {"", ".", ".."} or REPOSITORY_PART.fullmatch(part) is None
            for part in parts
        ):
            raise ValueError("repository must be OWNER/REPOSITORY")
        return cls(*parts)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.slug}.git"


@dataclass(frozen=True)
class StateLayout:
    root: Path
    project_id: str

    @classmethod
    def from_environment(
        cls, project_id: str, environment: Mapping[str, str] | None = None
    ) -> "StateLayout":
        env = os.environ if environment is None else environment
        if env.get("AGENT_CONTAINER_HOME"):
            root = Path(env["AGENT_CONTAINER_HOME"])
        elif env.get("XDG_DATA_HOME"):
            root = Path(env["XDG_DATA_HOME"]) / "agent-container"
        else:
            root = Path.home() / ".local/share/agent-container"
        if not root.is_absolute():
            raise ValueError("agent container state root must be absolute")
        return cls(root.resolve(), validate_project_id(project_id))

    @property
    def gh_dir(self) -> Path: return self.root / "gh"
    @property
    def codex_auth_dir(self) -> Path: return self.root / "shared-auth/codex"
    @property
    def codex_auth_file(self) -> Path: return self.codex_auth_dir / "auth.json"
    @property
    def project_dir(self) -> Path: return self.root / "projects" / self.project_id
    @property
    def codex_home(self) -> Path: return self.project_dir / "codex-home"
    @property
    def cache(self) -> Path: return self.project_dir / "cache"
    @property
    def project_file(self) -> Path: return self.project_dir / "project.json"
    @property
    def workspace(self) -> Path: return self.root / "workspaces" / self.project_id
```

Update `handover.py` to import `validate_project_id` from `agent_container.state` and remove its duplicate regex/function. Update the handover test import accordingly only if needed; observable behavior and error text must stay unchanged.

- [ ] **Step 4: Add failing permission, symlink, and metadata tests**

Extend `test_state.py`:

```python
from agent_container.state import ProjectRecord
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file


class StateSecurityTest(unittest.TestCase):
    def test_private_directory_rejects_group_access(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "auth"
            path.mkdir(mode=0o750)
            with self.assertRaisesRegex(PermissionError, "mode 0700"):
                ensure_private_directory(path)

    def test_private_file_rejects_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "real"
            target.write_text("fixture", encoding="utf-8")
            link = Path(temp) / "auth.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                ensure_private_file(link)

    def test_project_record_round_trips_without_credentials(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            record = ProjectRecord(
                repository=Repository.parse("jj1xgo/agent-container"),
                handover_root=Path(temp).resolve() / "handovers",
            )
            record.write(path)
            self.assertEqual(ProjectRecord.read(path), record)
            self.assertNotIn("token", path.read_text(encoding="utf-8").lower())
```

- [ ] **Step 5: Run the new security tests and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state.StateSecurityTest -v`

Expected: FAIL because `ProjectRecord` and permission helpers do not exist.

- [ ] **Step 6: Implement exact mode and metadata checks**

Add to `state.py`:

```python
from dataclasses import asdict
import json
import stat


def ensure_private_directory(path: Path, create: bool = False) -> Path:
    if path.is_symlink():
        raise ValueError(f"directory must not be a symlink: {path}")
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise FileNotFoundError(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise PermissionError(f"directory must have mode 0700: {path}")
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"directory must be owned by the current user: {path}")
    return path.resolve()


def ensure_private_file(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"credential file must not be a symlink: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"credential file must have mode 0600: {path}")
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"credential file must be owned by the current user: {path}")
    return path.resolve()


@dataclass(frozen=True)
class ProjectRecord:
    repository: Repository
    handover_root: Path

    def write(self, path: Path) -> None:
        payload = {
            "repository": self.repository.slug,
            "handover_root": str(self.handover_root),
        }
        with path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        path.chmod(0o600)

    @classmethod
    def read(cls, path: Path) -> "ProjectRecord":
        ensure_private_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"repository", "handover_root"}:
            raise ValueError("project metadata has unexpected fields")
        handover_root = Path(payload["handover_root"])
        if not handover_root.is_absolute():
            raise ValueError("handover_root must be absolute")
        return cls(Repository.parse(payload["repository"]), handover_root.resolve())
```

- [ ] **Step 7: Run state and existing handover tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state tests.codex.test_handover -v`

Expected: PASS with all state and handover tests.

- [ ] **Step 8: Commit the state boundary**

```bash
git add src/agent_container/state.py src/agent_container/handover.py tests/container tests/codex/test_handover.py
git commit -m "feat: add isolated agent state boundary"
```

### Task 2: Managed Codex profile and container image contract

**Files:**
- Create: `src/agent_container/profile.py`
- Create: `tests/container/test_profile.py`
- Create: `tests/container/test_image.py`
- Create: `Containerfile`
- Create: `.containerignore`
- Create: `bin/agentctl`
- Modify: `profiles/codex/config.toml`
- Modify: `tests/codex/test_statusline.py`

**Interfaces:**
- Consumes: repository `profiles/codex` directory and a newly created Codex home.
- Produces: `seed_codex_home(profile_root: Path, codex_home: Path) -> None` and image `localhost/agent-container:dev` with `/opt/agent-container/src` and `/opt/agent-container/profiles/codex`.

- [ ] **Step 1: Write failing profile tests**

Create `tests/container/test_profile.py`:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.profile import seed_codex_home


ROOT = Path(__file__).resolve().parents[2]


class ProfileSeedTest(unittest.TestCase):
    def test_seed_copies_managed_files_and_records_version(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertTrue((codex_home / "config.toml").is_file())
            self.assertTrue((codex_home / "hooks.json").is_file())
            self.assertTrue((codex_home / "skills/handover/SKILL.md").is_file())
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(encoding="utf-8"),
                "1\n",
            )

    def test_seed_refuses_to_overwrite_existing_config(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("existing\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "config.toml"):
                seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertEqual(
                (codex_home / "config.toml").read_text(encoding="utf-8"), "existing\n"
            )
```

Extend `tests/codex/test_statusline.py` to assert:

```python
self.assertEqual(config["cli_auth_credentials_store"], "file")
self.assertEqual(config["forced_login_method"], "chatgpt")
```

- [ ] **Step 2: Run profile tests and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_profile tests.codex.test_statusline -v`

Expected: FAIL because `agent_container.profile` and auth settings do not exist.

- [ ] **Step 3: Implement non-overwriting profile seed**

Create `profile.py`:

```python
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
        existing = next(target for _, target in sources if target.exists() or target.is_symlink())
        raise FileExistsError(f"managed profile target already exists: {existing}")
    codex_home.mkdir(parents=True, mode=0o700)
    shutil.copy2(sources[0][0], sources[0][1])
    shutil.copy2(sources[1][0], sources[1][1])
    shutil.copytree(sources[2][0], sources[2][1], symlinks=False)
    (codex_home / "managed-profile.version").write_text(PROFILE_VERSION, encoding="utf-8")
```

Add the two top-level auth settings before `[tui]` in `profiles/codex/config.toml`.

- [ ] **Step 4: Run profile tests and confirm GREEN**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_profile tests.codex.test_statusline -v`

Expected: PASS.

- [ ] **Step 5: Write failing static image contract tests**

Create `tests/container/test_image.py`:

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ContainerImageContractTest(unittest.TestCase):
    def test_image_pins_codex_and_runs_as_agent(self) -> None:
        body = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("ARG CODEX_VERSION=0.149.0", body)
        self.assertIn("@openai/codex@${CODEX_VERSION}", body)
        self.assertIn("USER agent", body)
        self.assertIn("WORKDIR /workspace", body)
        self.assertIn("COPY src /opt/agent-container/src", body)
        self.assertIn("COPY profiles/codex /opt/agent-container/profiles/codex", body)

    def test_containerignore_excludes_git_and_local_state(self) -> None:
        patterns = (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines()
        for required in (".git", ".worktrees", ".codex", "auth.json", "__pycache__"):
            self.assertIn(required, patterns)
```

- [ ] **Step 6: Run image contract test and confirm RED**

Run: `python3 -m unittest tests.container.test_image -v`

Expected: FAIL with `FileNotFoundError` for `Containerfile`.

- [ ] **Step 7: Add the concrete image and wrapper**

Create `Containerfile`:

```dockerfile
FROM docker.io/library/node:22-bookworm-slim

ARG CODEX_VERSION=0.149.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gh git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}"

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /opt/agent-container /workspace \
    && chown agent:agent /workspace

COPY src /opt/agent-container/src
COPY profiles/codex /opt/agent-container/profiles/codex

ENV HOME=/home/agent \
    PYTHONPATH=/opt/agent-container/src \
    GH_CONFIG_DIR=/home/agent/.config/gh

USER agent
WORKDIR /workspace
CMD ["codex"]
```

Create `.containerignore` with exactly:

```text
.git
.worktrees
.codex
auth.json
__pycache__
*.pyc
```

Create executable `bin/agentctl`:

```sh
#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m agent_container.agentctl "$@"
```

- [ ] **Step 8: Run profile and image contract tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_profile tests.container.test_image tests.codex.test_statusline -v`

Expected: PASS.

- [ ] **Step 9: Commit the managed profile and image**

```bash
git add Containerfile .containerignore bin/agentctl profiles/codex/config.toml src/agent_container/profile.py tests/container tests/codex/test_statusline.py
git commit -m "feat: add managed Codex container image"
```

### Task 3: Secret-free Podman command builder

**Files:**
- Create: `src/agent_container/podman.py`
- Create: `tests/container/test_podman.py`

**Interfaces:**
- Consumes: `StateLayout`、`Repository`、image name、repo root、handover project directory、host UID/GID。
- Produces: immutable `CommandSpec(argv: tuple[str, ...], environment: dict[str, str])` and `build_image_spec`、`auth_codex_spec`、`clone_project_spec`、`run_codex_spec`、`run_command`.

- [ ] **Step 1: Write failing command builder tests**

Create `tests/container/test_podman.py` with a reusable temporary layout and these assertions:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.podman import auth_codex_spec
from agent_container.podman import build_image_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import run_codex_spec
from agent_container.state import Repository
from agent_container.state import StateLayout


IMAGE = "localhost/agent-container:dev"


class PodmanCommandTest(unittest.TestCase):
    def test_build_uses_only_repository_context(self) -> None:
        spec = build_image_spec(Path("/repo"), IMAGE)
        self.assertEqual(
            spec.argv,
            ("podman", "build", "--tag", IMAGE, "--file", "/repo/Containerfile", "/repo"),
        )

    def test_auth_mounts_only_shared_codex_auth_directory(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = auth_codex_spec(layout, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertIn("codex login --device-auth", joined)
        self.assertNotIn("/workspace", joined)

    def test_run_has_hardened_flags_and_narrow_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = run_codex_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=1000,
            gid=1000,
        )
        joined = " ".join(spec.argv)
        for required in ("--rm", "--read-only", "--cap-drop=all", "no-new-privileges"):
            self.assertIn(required, spec.argv if required != "no-new-privileges" else joined)
        self.assertIn("src=/state/workspaces/agent-container,dst=/workspace", joined)
        self.assertIn("src=/vault/handovers/agent-container,dst=/handovers/agent-container", joined)
        self.assertNotIn("/vault,dst=", joined)
        self.assertNotIn("token", joined.lower())
```

Add clone assertions that the gh directory is read-only, the repository slug appears but no credential value does, and the destination is `/workspaces/agent-container`.

- [ ] **Step 2: Run command tests and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: FAIL because `agent_container.podman` does not exist.

- [ ] **Step 3: Implement CommandSpec and shared hardening arguments**

Create `podman.py` with:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
import os
import subprocess

from agent_container.state import Repository, StateLayout


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    environment: dict[str, str]


def _mount(source: Path, target: str, read_only: bool = False) -> str:
    options = f"type=bind,src={source},dst={target}"
    return f"{options},ro=true" if read_only else options


def _runtime_prefix(uid: int, gid: int) -> list[str]:
    return [
        "podman", "run", "--rm", "--interactive", "--tty", "--read-only",
        "--cap-drop=all", "--security-opt=no-new-privileges",
        f"--userns=keep-id:uid=1000,gid=1000",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
    ]


def _git_environment_args() -> list[str]:
    return [
        "--env", "GH_CONFIG_DIR=/home/agent/.config/gh",
        "--env", "GIT_CONFIG_COUNT=1",
        "--env", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "--env", "GIT_CONFIG_VALUE_0=!gh auth git-credential",
    ]
```

The `uid` and `gid` arguments must be checked against `os.getuid()` and `os.getgid()` by the caller; the fixed inner UID/GID `1000` matches image user `agent`. Do not interpolate a token or config file body into `argv` or `environment`.

- [ ] **Step 4: Implement the four exact command builders and runner**

```python
def build_image_spec(repo_root: Path, image: str) -> CommandSpec:
    root = repo_root.resolve()
    return CommandSpec(
        ("podman", "build", "--tag", image, "--file", str(root / "Containerfile"), str(root)),
        {},
    )


def auth_codex_spec(layout: StateLayout, image: str) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _mount(layout.codex_auth_dir, "/home/agent/.codex")]
    argv += [image, "codex", "login", "--device-auth"]
    return CommandSpec(tuple(argv), {})


def clone_project_spec(layout: StateLayout, repository: Repository, image: str) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += _git_environment_args()
    argv += ["--mount", _mount(layout.gh_dir, "/home/agent/.config/gh", True)]
    argv += ["--mount", _mount(layout.root / "workspaces", "/workspaces")]
    argv += [image, "gh", "repo", "clone", repository.slug, f"/workspaces/{layout.project_id}"]
    return CommandSpec(tuple(argv), {})


def run_codex_spec(
    layout: StateLayout,
    handover_project: Path,
    image: str,
    uid: int,
    gid: int,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    argv = _runtime_prefix(uid, gid)
    argv += _git_environment_args()
    argv += ["--env", "AGENT_HANDOVER_ROOT=/handovers"]
    mounts = (
        (layout.workspace, "/workspace", False),
        (layout.codex_home, "/home/agent/.codex", False),
        (layout.codex_auth_file, "/home/agent/.codex/auth.json", False),
        (layout.cache, "/home/agent/.cache", False),
        (layout.gh_dir, "/home/agent/.config/gh", True),
        (handover_project, f"/handovers/{layout.project_id}", False),
    )
    for source, target, read_only in mounts:
        argv += ["--mount", _mount(source, target, read_only)]
    argv += ["--env", f"AGENT_PROJECT_ID={layout.project_id}", image, "codex"]
    return CommandSpec(tuple(argv), {})


def run_command(spec: CommandSpec, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(spec.environment)
    return subprocess.run(spec.argv, env=environment, text=True, check=check)
```

- [ ] **Step 5: Run Podman builder tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: PASS. Inspect failing string assertions rather than weakening hardening or mount scope.

- [ ] **Step 6: Commit the command boundary**

```bash
git add src/agent_container/podman.py tests/container/test_podman.py
git commit -m "feat: build hardened Podman commands"
```

### Task 4: Build and Codex authentication CLI

**Files:**
- Create: `src/agent_container/agentctl.py`
- Create: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: Task 1 layout, Task 2 profile seed, Task 3 command specs, injected `runner: Callable[[CommandSpec], CompletedProcess]`.
- Produces: `parser() -> argparse.ArgumentParser`、`main(argv=None, environment=None, runner=run_command, git_remote_reader=read_git_remote, stdout=sys.stdout, stderr=sys.stderr) -> int`。Task 4では`build`と`auth codex`を実装し、同じdependency injection interfaceをTask 5-6でも維持する。

- [ ] **Step 1: Write failing parser and fake-runner tests**

Create `tests/container/test_agentctl.py`:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import subprocess
import unittest

from agent_container.agentctl import main


class AgentCtlBuildAuthTest(unittest.TestCase):
    def test_build_runs_one_podman_build(self) -> None:
        calls = []
        result = main(["build"], runner=lambda spec: calls.append(spec) or subprocess.CompletedProcess(spec.argv, 0))
        self.assertEqual(result, 0)
        self.assertEqual(calls[0].argv[:2], ("podman", "build"))

    def test_auth_creates_private_state_and_runs_device_login(self) -> None:
        with TemporaryDirectory() as temp:
            calls = []
            environment = {"AGENT_CONTAINER_HOME": temp}

            def runner(spec):
                calls.append(spec)
                auth_file = Path(temp) / "shared-auth/codex/auth.json"
                auth_file.write_text("fixture-not-a-token", encoding="utf-8")
                auth_file.chmod(0o600)
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(
                ["auth", "codex"],
                environment=environment,
                runner=runner,
            )
            self.assertEqual(result, 0)
            self.assertEqual((Path(temp) / "shared-auth/codex").stat().st_mode & 0o777, 0o700)
            self.assertIn("--device-auth", calls[0].argv)
```

- [ ] **Step 2: Run CLI test and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: FAIL because `agent_container.agentctl` does not exist.

- [ ] **Step 3: Implement parser and build/auth orchestration**

Implement the parser with exact subcommands:

```python
def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agentctl")
    command.add_argument("--image", default="localhost/agent-container:dev")
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("build")
    auth = subcommands.add_parser("auth")
    auth.add_subparsers(dest="agent", required=True).add_parser("codex")
    project = subcommands.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command", required=True)
    add = project_subcommands.add_parser("add")
    add.add_argument("repository")
    add.add_argument("--project")
    add.add_argument("--handover-root", type=Path, required=True)
    run = subcommands.add_parser("run")
    run.add_argument("project")
    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("project")
    return command
```

In `main`, locate repository root from `Path(__file__).resolve().parents[2]`, create only `root`, `shared-auth`, and `shared-auth/codex` with mode `0700` for auth, seed the shared auth home from `profiles/codex` only when no managed files exist, run `auth_codex_spec`, then always require `auth.json` to exist with mode `0600`. Test runners must create a mode `0600` fixture file to satisfy the same postcondition as production.

Catch `ValueError`, `PermissionError`, `FileNotFoundError`, and `subprocess.CalledProcessError` at the outer CLI boundary, print one line to stderr without environment/config contents, and return the subprocess code or `1`.

- [ ] **Step 4: Run build/auth tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: PASS.

- [ ] **Step 5: Add a regression test that errors never print credential content**

Use a temporary `auth.json` containing the non-token string `DO-NOT-PRINT-CREDENTIAL-BODY`, mode `0644`, call `main(["auth", "codex"], ...)` while capturing stderr, and assert the marker is absent while `mode 0600` is present.

- [ ] **Step 6: Run the complete agentctl test module**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v`

Expected: PASS.

- [ ] **Step 7: Commit build and authentication commands**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: add image build and Codex login commands"
```

### Task 5: Isolated project registration and clone

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `src/agent_container/state.py`
- Modify: `tests/container/test_agentctl.py`
- Modify: `tests/container/test_state.py`

**Interfaces:**
- Consumes: `agentctl project add OWNER/REPOSITORY --handover-root ABSOLUTE_PATH [--project ID]`.
- Produces: private project directories, cloned workspace, `project.json`, seeded project Codex home; `validate_workspace_origin(workspace, repository, git_runner=subprocess.run) -> None`.

- [ ] **Step 1: Write failing successful registration test**

Add a fake runner that creates the workspace `.git` directory when it receives `gh repo clone`, and inject a fake git runner returning the expected origin:

```python
def test_project_add_records_repository_after_clone(self) -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp) / "state"
        handovers = Path(temp) / "handovers"
        (handovers / "agent-container").mkdir(parents=True)

        def runner(spec):
            workspace = root / "workspaces/agent-container"
            workspace.mkdir(parents=True)
            (workspace / ".git").mkdir()
            return subprocess.CompletedProcess(spec.argv, 0)

        result = main(
            ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
            environment={"AGENT_CONTAINER_HOME": str(root)},
            runner=runner,
            git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
        )
        self.assertEqual(result, 0)
        record = ProjectRecord.read(root / "projects/agent-container/project.json")
        self.assertEqual(record.repository.slug, "jj1xgo/agent-container")
        self.assertTrue((root / "projects/agent-container/codex-home/config.toml").is_file())
```

- [ ] **Step 2: Run registration test and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlProjectTest.test_project_add_records_repository_after_clone -v`

Expected: FAIL because project add dispatch is not implemented.

- [ ] **Step 3: Implement origin normalization without broad URL acceptance**

Add to `state.py`:

```python
def validate_workspace_origin(workspace: Path, repository: Repository, remote_url: str) -> None:
    if workspace.is_symlink() or not (workspace / ".git").is_dir():
        raise ValueError(f"workspace is not a safe Git repository: {workspace}")
    allowed = {
        repository.https_url,
        repository.https_url.removesuffix(".git"),
    }
    if remote_url.strip() not in allowed:
        raise ValueError(
            f"workspace origin does not match {repository.slug}: {workspace}"
        )
```

The production `git_remote_reader` must run `git -C <workspace> remote get-url origin` with `capture_output=True`, `text=True`, `check=True`; it must never invoke checkout, reset, clean, or fetch.

- [ ] **Step 4: Implement project add in fail-safe order**

The concrete order is:

1. Parse repository and project ID.
2. Resolve and validate handover root and `<handover-root>/<project>`; reject symlinks.
3. Create private state root, `gh`, project directory, cache, codex home parent, and workspaces parent.
4. Require `gh/hosts.yml` mode `0600` before clone.
5. If workspace is absent, run `clone_project_spec` once. If it exists, do not run clone.
6. Validate `.git` and exact HTTPS origin.
7. Seed project Codex home only if empty.
8. Write `project.json` with exclusive create only after all validation passes.

If clone fails or origin mismatches, leave the workspace for manual inspection and do not write `project.json`.

- [ ] **Step 5: Add non-destructive failure tests**

Add tests proving:

- an existing ordinary directory is not removed or overwritten;
- a mismatched origin returns `1` and does not create metadata;
- a symlinked handover project is rejected before the runner is called;
- a failed clone leaves its partial marker file untouched;
- `main`, `master`, and any branch are not checked out or changed by project registration.

Use explicit marker files such as `KEEP-ME` and assert their content after failure.

- [ ] **Step 6: Run state and project tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state tests.container.test_agentctl.AgentCtlProjectTest -v`

Expected: PASS.

- [ ] **Step 7: Commit isolated project registration**

```bash
git add src/agent_container/state.py src/agent_container/agentctl.py tests/container/test_state.py tests/container/test_agentctl.py
git commit -m "feat: clone projects into isolated workspaces"
```

### Task 6: Runtime launch and safe doctor

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `src/agent_container/podman.py`
- Modify: `tests/container/test_agentctl.py`
- Modify: `tests/container/test_podman.py`

**Interfaces:**
- Consumes: `agentctl run PROJECT` and `agentctl doctor PROJECT`.
- Produces: runtime Codex launch with validated mounts; doctor lines in `PASS|WARN|FAIL  check: detail` form and nonzero exit only for FAIL.

- [ ] **Step 1: Write failing run orchestration test**

Build a complete temporary state with mode `0700` directories, mode `0600` `auth.json`, `hosts.yml`, and `project.json`, plus `.git` workspace and handover project. Inject a remote reader with the expected URL, then assert:

```python
result = main(
    ["run", "agent-container"],
    environment={"AGENT_CONTAINER_HOME": str(root)},
    runner=runner,
    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
)
self.assertEqual(result, 0)
self.assertIn("codex", calls[0].argv)
self.assertIn("AGENT_PROJECT_ID=agent-container", calls[0].argv)
```

- [ ] **Step 2: Run the run test and confirm RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest.test_run_validates_then_starts_codex -v`

Expected: FAIL because run dispatch is not implemented.

- [ ] **Step 3: Implement runtime preflight and launch**

Before building `run_codex_spec`, `run` must:

- read private `project.json`;
- validate state root, project directory, codex home, cache, gh directory, auth file, `gh/hosts.yml`;
- validate workspace `.git` and exact origin;
- validate handover root and selected project directory, rejecting symlinks;
- require current process UID/GID and use them only for the keep-id check;
- run exactly one Podman command after all checks pass.

Do not print the complete Podman argv in normal output because absolute auth paths are operational metadata. Print only `Starting Codex for project: agent-container`.

- [ ] **Step 4: Write failing doctor output test**

```python
def test_doctor_reports_presence_without_secret_values(self) -> None:
    output = io.StringIO()
    result = main(
        ["doctor", "agent-container"],
        environment={"AGENT_CONTAINER_HOME": str(root)},
        runner=doctor_runner,
        git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
        stdout=output,
    )
    self.assertEqual(result, 0)
    rendered = output.getvalue()
    self.assertIn("PASS  podman-rootless", rendered)
    self.assertIn("PASS  codex-auth: present, mode 0600", rendered)
    self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", rendered)
```

- [ ] **Step 5: Implement doctor with fixed checks**

Doctor runs these checks in order and prints no file contents:

1. `podman --version` exits zero.
2. `podman info --format {{.Host.Security.Rootless}}` returns `true`.
3. `podman image exists <image>` exits zero.
4. state root and private directory modes pass.
5. Codex auth file and gh hosts file exist with mode `0600`.
6. project metadata parses.
7. workspace origin matches.
8. selected handover project is a real directory, not a symlink.
9. `network-policy` prints WARN with `outbound network is not domain-restricted in Phase 1`.

Use a small immutable `CheckResult(level: str, name: str, detail: str)` type. Exit `1` when any level is `FAIL`; WARN alone exits `0`.

- [ ] **Step 6: Add refusal tests for every preflight boundary**

Individually replace auth file, gh directory, workspace, and handover project with symlinks or broad modes. For each case assert the runtime runner is never called, exit is `1`, and credential fixture content is absent from stderr/stdout.

- [ ] **Step 7: Run all container tests**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests/container -v`

Expected: PASS with no real Podman process started.

- [ ] **Step 8: Run the full existing suite**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS; existing statusline and handover behavior remains unchanged.

- [ ] **Step 9: Commit runtime and doctor**

```bash
git add src/agent_container/agentctl.py src/agent_container/podman.py tests/container
git commit -m "feat: launch and diagnose isolated Codex containers"
```

### Task 7: Operator documentation and real-host smoke test

**Files:**
- Create: `docs/phase1-codex-container.md`
- Create: `docs/phase1-smoke-test.md`
- Modify: `README.md`
- Modify: `docs/codex-operations.md`

**Interfaces:**
- Consumes: Tasks 1-6 command interface and approved Phase 1 spec.
- Produces: Japanese operator guide, explicit smoke-test checklist, recorded observed results without credential values.

- [ ] **Step 1: Write a failing documentation contract test**

Create `tests/container/test_docs.py`:

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class Phase1DocumentationTest(unittest.TestCase):
    def test_operator_guide_contains_complete_safe_flow(self) -> None:
        body = (ROOT / "docs/phase1-codex-container.md").read_text(encoding="utf-8")
        for command in (
            "agentctl build",
            "agentctl auth codex",
            "agentctl project add",
            "agentctl doctor",
            "agentctl run",
        ):
            self.assertIn(command, body)
        self.assertIn("外向き通信はドメイン制限されていません", body)
        self.assertIn("~/.codex をmountしません", body)

    def test_smoke_guide_forbids_main_and_secret_output(self) -> None:
        body = (ROOT / "docs/phase1-smoke-test.md").read_text(encoding="utf-8")
        self.assertIn("mainへ直接pushしない", body)
        self.assertIn("credential本文を表示しない", body)
        self.assertIn("codex login status", body)
        self.assertIn("gh auth status", body)
```

- [ ] **Step 2: Run documentation test and confirm RED**

Run: `python3 -m unittest tests.container.test_docs -v`

Expected: FAIL because the two documents do not exist.

- [ ] **Step 3: Write the operator guide with exact first-run commands**

Document this flow, using the actual handover root only as an example and never embedding tokens:

```bash
export AGENT_CONTAINER_HOME="$HOME/.local/share/agent-container"
bin/agentctl build
bin/agentctl auth codex
bin/agentctl project add jj1xgo/agent-container \
  --handover-root "$HOME/obsidian-vault/handovers"
bin/agentctl doctor agent-container
bin/agentctl run agent-container
```

Explain that `auth codex` uses device code, `gh` was prepared separately, credential files require `0600`, directories require `0700`, project registration never overwrites an existing workspace, and Phase 1 outbound network is not domain-restricted.

- [ ] **Step 4: Write the smoke test checklist**

The checklist must contain observable expected results for:

- `podman info` rootless true;
- image build;
- private clone to dedicated workspace;
- container `gh auth status` with masked token output only;
- `codex login status` without `auth.json` content;
- TUI `/hooks` trust;
- statusline fields;
- latest handover path notification without body;
- container restart and `/resume`;
- a named test branch push and PR creation after separate user approval;
- verification that host `~/.codex`, host workspace, other handovers, Podman socket are absent from container mounts.

Add a results table with rows for command/check, expected result, observed result, date. Use `not run` as the initial observed value; this is an explicit test status, not an implementation placeholder.

- [ ] **Step 5: Update README and Codex operations guide**

README should link the Phase 1 design, operator guide, smoke test, and PR workflow. `docs/codex-operations.md` should state that prototype unit tests do not prove authenticated TUI behavior and point to the real-host checklist.

- [ ] **Step 6: Run documentation and full unit tests**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 7: Build the real image with explicit approval**

Run: `bin/agentctl build`

Expected: Podman build exits `0`, image `localhost/agent-container:dev` exists, and build output contains no host credential path or value.

- [ ] **Step 8: Run static doctor before authentication**

Run: `bin/agentctl doctor agent-container`

Expected before remaining bootstrap: explicit FAIL entries for missing state, not a traceback and not credential content. Record the actual output category in the smoke guide.

- [ ] **Step 9: Perform Codex device authentication with the user present**

Run: `bin/agentctl auth codex`

Expected: user completes device code in a browser; `shared-auth/codex/auth.json` exists with mode `0600`; `codex login status` reports ChatGPT authentication. Do not run `cat`, `jq`, `sed`, or token-display commands on `auth.json`.

- [ ] **Step 10: Register and diagnose the test project**

Run:

```bash
bin/agentctl project add jj1xgo/agent-container \
  --handover-root "$HOME/obsidian-vault/handovers"
bin/agentctl doctor agent-container
```

Expected: clone is under the dedicated state root, all required checks PASS, and only the documented network-policy WARN remains.

- [ ] **Step 11: Run the authenticated TUI checks**

Run: `bin/agentctl run agent-container`

Inside Codex, perform `/hooks`, `/statusline`, `/status`, `/resume`, and verify the handover path notification. Exit normally, run the container again, and verify session continuity. Record only account method, field presence, paths, and pass/fail; never record credential values or transcript content.

- [ ] **Step 12: Validate shared auth file update compatibility**

Before and after a normal authenticated Codex request, record only `stat` metadata for `shared-auth/codex/auth.json`—owner, mode, size, modification time—and run `codex login status`. Expected: authentication remains valid and no bind-mount write error occurs. If any write/rename error occurs, stop implementation and return to the approved design; do not copy credentials into each project.

- [ ] **Step 13: Ask separately before GitHub mutation smoke test**

After user approval, create a branch named `test/phase1-container-smoke`, make a non-code test marker change agreed with the user, run tests, push the branch, and create a PR titled `Phase 1 container smoke test`. Do not merge it. If a branch/PR with that name already exists, stop and report it instead of overwriting.

- [ ] **Step 14: Update observed smoke results and commit**

Replace each `not run` cell that was actually executed with date, exact command category, exit status, and a secret-free result. Leave unexecuted rows as `not run`; do not claim they passed.

```bash
git add README.md docs/phase1-codex-container.md docs/phase1-smoke-test.md docs/codex-operations.md tests/container/test_docs.py
git commit -m "docs: add Phase 1 container operations"
```

- [ ] **Step 15: Final verification before PR update**

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check main...HEAD
git status --short --branch
```

Expected: all tests PASS, diff check exits `0`, and the worktree is clean on `feat/codex-operations-prototype` before push.
