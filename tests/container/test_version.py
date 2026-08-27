from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

import agent_container
from agent_container.version import resolve_version


ROOT = Path(__file__).resolve().parents[2]


class DevelopmentVersionTest(unittest.TestCase):
    def _git(self, root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    def _commit(self, root: Path, message: str, body: str) -> None:
        (root / "tracked.txt").write_text(body, encoding="utf-8")
        self._git(root, "add", "tracked.txt")
        self._git(root, "commit", "-m", message)

    def test_resolves_first_parent_distance_and_commit_identity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.1.0")
            self._commit(root, "next", "next\n")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(
                resolve_version(
                    root,
                    {"AGENT_CONTAINER_VERSION": "9.9.9"},
                ),
                f"0.2.0-dev.1+g{sha}",
            )

    def test_first_parent_distance_excludes_merged_branch_commits(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.1.0")
            self._git(root, "switch", "-c", "feature")
            (root / "feature.txt").write_text("feature\n", encoding="utf-8")
            self._git(root, "add", "feature.txt")
            self._git(root, "commit", "-m", "feature")
            self._git(root, "switch", "main")
            self._commit(root, "main change", "main\n")
            self._git(root, "merge", "--no-ff", "feature", "-m", "merge feature")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.2.0-dev.2+g{sha}")

    def test_marks_tracked_changes_dirty_but_ignores_untracked_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.1.0")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")
            (root / "untracked.txt").write_text("ignored\n", encoding="utf-8")

            self.assertEqual(resolve_version(root), f"0.2.0-dev.0+g{sha}")

            (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(resolve_version(root), f"0.2.0-dev.0+g{sha}.dirty")

    def test_falls_back_when_git_release_metadata_is_unavailable(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(resolve_version(Path(temp), {}), "0.2.0-dev.0")

    def test_git_checkout_without_release_tag_still_uses_commit_identity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "shallow-style checkout", "checkout\n")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.2.0-dev.0+g{sha}")

    def test_prefers_version_embedded_in_built_image(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(
                resolve_version(
                    Path(temp),
                    {"AGENT_CONTAINER_VERSION": "0.2.0-dev.8+g123abcd"},
                ),
                "0.2.0-dev.8+g123abcd",
            )

    def test_rejects_embedded_versions_with_numeric_leading_zeroes(self) -> None:
        with TemporaryDirectory() as temp:
            for invalid in ("1.02.3", "1.2.3-01", "1.02.3-01+build"):
                with self.subTest(version=invalid):
                    self.assertEqual(
                        resolve_version(
                            Path(temp),
                            {"AGENT_CONTAINER_VERSION": invalid},
                        ),
                        "0.2.0-dev.0",
                    )

    def test_package_version_uses_checkout_metadata(self) -> None:
        self.assertEqual(agent_container.__version__, resolve_version(ROOT))


if __name__ == "__main__":
    unittest.main()
