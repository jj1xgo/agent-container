from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

import agent_container
from agent_container.release_metadata import (
    DEVELOPMENT_BASE_TAG,
    DEVELOPMENT_VERSION,
    FALLBACK_VERSION,
    RELEASE_TAG,
    RELEASE_VERSION,
)
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

    def test_release_metadata_is_internally_consistent(self) -> None:
        self.assertEqual(RELEASE_TAG, "v0.5.0")
        self.assertEqual(RELEASE_VERSION, "0.5.0")
        self.assertEqual(DEVELOPMENT_BASE_TAG, "v0.4.0")
        self.assertEqual(DEVELOPMENT_VERSION, "0.5.0-dev")
        self.assertEqual(FALLBACK_VERSION, "0.5.0-dev.0")

    def test_exact_release_tag_returns_release_version(self) -> None:
        for tag_arguments in (
            ("v0.5.0",),
            ("-a", "v0.5.0", "-m", "release"),
        ):
            with self.subTest(tag_arguments=tag_arguments), TemporaryDirectory() as temp:
                root = Path(temp)
                self._git(root, "init", "-b", "main")
                self._git(root, "config", "user.name", "Version Test")
                self._git(root, "config", "user.email", "version@example.invalid")
                self._commit(root, "release", "release\n")
                self._git(root, "tag", "v0.4.0")
                self._commit(root, "candidate", "candidate\n")
                self._git(root, "tag", *tag_arguments)

                self.assertEqual(resolve_version(root), "0.5.0")

    def test_post_release_commit_uses_next_development_version(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.4.0")
            self._commit(root, "next", "next\n")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.5.0-dev.1+g{sha}")

    def test_resolves_first_parent_distance_and_commit_identity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.4.0")
            self._commit(root, "next", "next\n")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(
                resolve_version(
                    root,
                    {"AGENT_CONTAINER_VERSION": "9.9.9"},
                ),
                f"0.5.0-dev.1+g{sha}",
            )

    def test_first_parent_distance_excludes_merged_branch_commits(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.4.0")
            self._git(root, "switch", "-c", "feature")
            (root / "feature.txt").write_text("feature\n", encoding="utf-8")
            self._git(root, "add", "feature.txt")
            self._git(root, "commit", "-m", "feature")
            self._git(root, "switch", "main")
            self._commit(root, "main change", "main\n")
            self._git(root, "merge", "--no-ff", "feature", "-m", "merge feature")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.5.0-dev.2+g{sha}")

    def test_marks_tracked_changes_dirty_but_ignores_untracked_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "release", "release\n")
            self._git(root, "tag", "v0.4.0")
            self._commit(root, "candidate", "candidate\n")
            self._git(root, "tag", "v0.5.0")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")
            (root / "untracked.txt").write_text("ignored\n", encoding="utf-8")

            self.assertEqual(resolve_version(root), "0.5.0")

            (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(resolve_version(root), f"0.5.0-dev.1+g{sha}.dirty")

    def test_falls_back_when_git_release_metadata_is_unavailable(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(resolve_version(Path(temp), {}), "0.5.0-dev.0")

    def test_git_checkout_without_release_tag_still_uses_commit_identity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "shallow-style checkout", "checkout\n")
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.5.0-dev.0+g{sha}")

    def test_genuinely_shallow_checkout_keeps_commit_identity_at_zero_distance(self) -> None:
        with TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            shallow = Path(temp) / "shallow"
            source.mkdir()
            self._git(source, "init", "-b", "main")
            self._git(source, "config", "user.name", "Version Test")
            self._git(source, "config", "user.email", "version@example.invalid")
            self._commit(source, "base", "base\n")
            self._git(source, "tag", "v0.4.0")
            self._commit(source, "next", "next\n")
            self._git(source, "clone", "--depth", "1", f"file://{source}", shallow)
            sha = self._git(shallow, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(self._git(shallow, "rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(resolve_version(shallow), f"0.5.0-dev.0+g{sha}")

    def test_unrelated_development_base_tag_keeps_commit_identity_at_zero_distance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "main"
            maintenance = Path(temp) / "maintenance"
            root.mkdir()
            maintenance.mkdir()
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Version Test")
            self._git(root, "config", "user.email", "version@example.invalid")
            self._commit(root, "main", "main\n")
            self._git(maintenance, "init", "-b", "release")
            self._git(maintenance, "config", "user.name", "Version Test")
            self._git(maintenance, "config", "user.email", "version@example.invalid")
            self._commit(maintenance, "maintenance", "maintenance\n")
            self._git(maintenance, "tag", "v0.4.0")
            self._git(
                root,
                "fetch",
                str(maintenance),
                "refs/tags/v0.4.0:refs/tags/v0.4.0",
            )
            sha = self._git(root, "rev-parse", "--short=7", "HEAD")

            self.assertEqual(resolve_version(root), f"0.5.0-dev.0+g{sha}")

    def test_prefers_version_embedded_in_built_image(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(
                resolve_version(
                    Path(temp),
                    {"AGENT_CONTAINER_VERSION": "0.2.0-dev.8+g123abcd"},
                ),
                "0.2.0-dev.8+g123abcd",
            )

    def test_empty_embedded_version_falls_back_to_release_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(
                resolve_version(
                    Path(temp),
                    {"AGENT_CONTAINER_VERSION": ""},
                ),
                "0.5.0-dev.0",
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
                        "0.5.0-dev.0",
                    )

    def test_package_version_uses_checkout_metadata(self) -> None:
        self.assertEqual(agent_container.__version__, resolve_version(ROOT))


if __name__ == "__main__":
    unittest.main()
