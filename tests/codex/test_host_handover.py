from pathlib import Path
import json
import os
import stat
from tempfile import mkstemp, TemporaryDirectory
import unittest

from agent_container.host_handover import publish_host_handover


VALID_BODY = """## 作業の目的
目的
## 現在地
現在地
## 決定事項と理由
決定
## 変更したファイル・commit・PR
変更
## 検証結果
検証
## 未解決事項とリスク
リスク
## 次の一手
次
"""


class HostHandoverPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        descriptor, name = mkstemp(prefix="agent-handover-", suffix=".md")
        os.close(descriptor)
        self.body_file = Path(name)
        self.body_file.write_text(VALID_BODY, encoding="utf-8")
        self.body_file.chmod(0o600)

    def tearDown(self) -> None:
        self.body_file.unlink(missing_ok=True)

    def _register(
        self,
        projects: Path,
        handovers: Path,
        project_id: str,
        repository: str = "owner/repository",
    ) -> Path:
        project_state = projects / project_id
        project_state.mkdir(parents=True)
        metadata = project_state / "project.json"
        metadata.write_text(
            json.dumps(
                {"repository": repository, "handover_root": str(handovers)}
            )
            + "\n",
            encoding="utf-8",
        )
        metadata.chmod(0o600)
        project_dir = handovers / project_id
        project_dir.mkdir(parents=True)
        return project_dir

    def test_publishes_only_to_the_preferred_registered_project(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "agent-container"
            projects = root / "state" / "projects"
            handovers = root / "handovers"
            workspace.mkdir()
            (workspace / ".git").mkdir()
            project_dir = self._register(
                projects, handovers, "agent-container"
            )
            created = publish_host_handover(
                cwd=workspace,
                projects_root=projects,
                title="Safe title",
                body_file=self.body_file,
                session_id="session-123",
                remote_getter=lambda _: "git@github.com:owner/repository.git",
            )

            self.assertEqual(created.parent, project_dir)
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
            document = created.read_text(encoding="utf-8")
            self.assertIn("# Handover: Safe title\n", document)
            self.assertIn("- Project: agent-container\n", document)
            self.assertIn("- Session: session-123\n", document)
            self.assertTrue(document.endswith("## 次の一手\n次\n"))

    def test_uses_only_unique_repository_match_as_fallback(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "linked-worktree"
            workspace.mkdir()
            projects = root / "projects"
            handovers = root / "handovers"
            expected = self._register(projects, handovers, "agent-container")
            self._register(
                projects, handovers, "unrelated", "another/repository"
            )

            created = publish_host_handover(
                cwd=workspace,
                projects_root=projects,
                title="Fallback",
                body_file=self.body_file,
                remote_getter=lambda _: "https://github.com/owner/repository.git",
            )

            self.assertEqual(created.parent, expected)

    def test_refuses_ambiguous_repository_registration(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "linked-worktree"
            workspace.mkdir()
            projects = root / "projects"
            handovers = root / "handovers"
            self._register(projects, handovers, "first")
            self._register(projects, handovers, "second")

            with self.assertRaisesRegex(ValueError, "unique registered project"):
                publish_host_handover(
                    cwd=workspace,
                    projects_root=projects,
                    title="Ambiguous",
                    body_file=self.body_file,
                    remote_getter=lambda _: "git@github.com:owner/repository.git",
                )

    def test_refuses_non_private_body_file(self) -> None:
        self.body_file.chmod(0o644)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "agent-container"
            workspace.mkdir()
            projects = root / "projects"
            handovers = root / "handovers"
            self._register(projects, handovers, "agent-container")

            with self.assertRaisesRegex(ValueError, "body file is invalid"):
                publish_host_handover(
                    cwd=workspace,
                    projects_root=projects,
                    title="Unsafe",
                    body_file=self.body_file,
                    remote_getter=lambda _: "git@github.com:owner/repository.git",
                )

    def test_refuses_body_file_outside_direct_temporary_root(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "agent-container"
            workspace.mkdir()
            projects = root / "projects"
            handovers = root / "handovers"
            self._register(projects, handovers, "agent-container")
            nested_body = root / "agent-handover-body.md"
            nested_body.write_text(VALID_BODY, encoding="utf-8")
            nested_body.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "body file is invalid"):
                publish_host_handover(
                    cwd=workspace,
                    projects_root=projects,
                    title="Unsafe",
                    body_file=nested_body,
                    remote_getter=lambda _: "git@github.com:owner/repository.git",
                )


if __name__ == "__main__":
    unittest.main()
