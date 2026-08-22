from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file
from agent_container.state import validate_project_id
from agent_container.state import validate_workspace_origin


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

    def test_workspace_origin_allows_only_repository_https_urls(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            (workspace / ".git").mkdir(parents=True)
            repository = Repository.parse("jj1xgo/agent-container")

            validate_workspace_origin(
                workspace, repository, "https://github.com/jj1xgo/agent-container.git\n"
            )
            validate_workspace_origin(
                workspace, repository, "https://github.com/jj1xgo/agent-container"
            )
            with self.assertRaisesRegex(ValueError, "origin does not match"):
                validate_workspace_origin(
                    workspace, repository, "git@github.com:jj1xgo/agent-container.git"
                )

    def test_workspace_origin_rejects_symlinked_workspace(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            (target / ".git").mkdir(parents=True)
            workspace = Path(temp) / "workspace"
            workspace.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "not a safe Git repository"):
                validate_workspace_origin(
                    workspace,
                    Repository.parse("jj1xgo/agent-container"),
                    "https://github.com/jj1xgo/agent-container.git",
                )
