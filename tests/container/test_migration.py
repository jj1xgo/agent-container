from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.migration import GeneratedFile
from agent_container.migration import add_plugin_entries
from agent_container.migration import apply_claude_migration
from agent_container.migration import plan_claude_migration
from agent_container.migration import render_migration_plan


SECRET_MARKER = "DO-NOT-PRINT-CREDENTIAL-BODY"


class MigrationTestCase(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "claude"
        (source / "hooks").mkdir(parents=True)
        (source / "skills/demo").mkdir(parents=True)
        (source / "CLAUDE.md").write_text("safe instructions\n", encoding="utf-8")
        (source / "settings.json").write_text(
            '{"permissions": {"allow": ["Read"]}}\n', encoding="utf-8"
        )
        (source / "hooks/run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (source / "hooks/run.sh").chmod(0o751)
        (source / "skills/demo/SKILL.md").write_text("safe skill\n", encoding="utf-8")

        denied = (
            ".credentials.json",
            ".claude.json",
            "projects",
            "sessions",
            "transcripts",
            "handovers",
            "plans",
            "state",
            "cache",
            "logs",
            "test-results",
            "scratchpad",
            ".git",
        )
        for name in denied:
            path = source / name
            if "." in name and not name.startswith("test-") and name != ".git":
                path.write_text(SECRET_MARKER, encoding="utf-8")
            else:
                path.mkdir()
                (path / "secret").write_text(SECRET_MARKER, encoding="utf-8")
        return source

    def assert_no_stage(self, destination: Path) -> None:
        self.assertEqual(
            list(destination.parent.glob(f".{destination.name}.migrate-*")), []
        )


class MigrationPlanningTest(MigrationTestCase):
    def test_plan_selects_only_allowlisted_paths_and_render_hides_denied_bodies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            plan = plan_claude_migration(source, root / "destination")

            self.assertEqual(
                {entry.relative_path.as_posix() for entry in plan.entries},
                {
                    "CLAUDE.md",
                    "settings.json",
                    "hooks",
                    "hooks/run.sh",
                    "skills",
                    "skills/demo",
                    "skills/demo/SKILL.md",
                },
            )
            self.assertEqual(
                tuple(entry.relative_path.as_posix() for entry in plan.entries),
                tuple(
                    sorted(entry.relative_path.as_posix() for entry in plan.entries)
                ),
            )
            rendered = render_migration_plan(plan)
            self.assertNotIn(SECRET_MARKER, "\n".join(rendered))
            self.assertIn("COPY executable hooks/run.sh", rendered)
            self.assertIn(f"DESTINATION {(root / 'destination').as_posix()}", rendered)
            self.assertTrue(
                {
                    "SKIP denied .credentials.json",
                    "SKIP denied .claude.json",
                    "SKIP denied .git",
                    "SKIP denied cache",
                    "SKIP denied handovers",
                    "SKIP denied logs",
                    "SKIP denied plans",
                    "SKIP denied projects",
                    "SKIP denied scratchpad",
                    "SKIP denied sessions",
                    "SKIP denied state",
                    "SKIP denied test-results",
                    "SKIP denied transcripts",
                }.issubset(rendered)
            )

    def test_plan_skips_denied_names_at_every_depth(self) -> None:
        nested_denied = (
            ".credentials.json",
            ".claude.json",
            ".git",
            "projects",
            "sessions",
            "transcripts",
            "handovers",
            "plans",
            "state",
            "cache",
            "logs",
            "test-results",
            "scratchpad",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            nested_root = source / "skills/demo"
            for name in nested_denied:
                denied = nested_root / name
                if name.endswith(".json"):
                    denied.write_text(SECRET_MARKER, encoding="utf-8")
                else:
                    denied.mkdir()
                    (denied / "secret").write_text(SECRET_MARKER, encoding="utf-8")

            plan = plan_claude_migration(source, root / "destination")

            selected = {entry.relative_path.as_posix() for entry in plan.entries}
            self.assertFalse(
                any(
                    f"/{name}" in f"/{relative_path}"
                    for name in nested_denied
                    for relative_path in selected
                )
            )
            self.assertTrue(
                {
                    Path("skills/demo") / name for name in nested_denied
                }.issubset(set(plan.skipped))
            )
            self.assertNotIn(SECRET_MARKER, "\n".join(render_migration_plan(plan)))

    def test_settings_must_be_an_object(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            (source / "settings.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON object"):
                plan_claude_migration(source, root / "destination")

    def test_settings_reject_api_key_helper_at_every_nesting_level(self) -> None:
        for payload in (
            '{"apiKeyHelper": "do-not-leak"}',
            '{"nested": [{"apiKeyHelper": "do-not-leak"}]}',
        ):
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = self.make_source(root)
                (source / "settings.json").write_text(payload, encoding="utf-8")

                with self.assertRaises(ValueError) as caught:
                    plan_claude_migration(source, root / "destination")
                self.assertIn("apiKeyHelper", str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_settings_reject_sensitive_environment_names_without_values(self) -> None:
        for name in (
            "ACCESS_TOKEN",
            "CLIENT_SECRET",
            "PASSWORD_FILE",
            "CREDENTIAL_PATH",
            "SERVICE_API_KEY",
            "AUTH_HEADER",
        ):
            with self.subTest(name=name), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = self.make_source(root)
                (source / "settings.json").write_text(
                    '{"nested": [{"env": {"'
                    + name
                    + '": "do-not-leak"}}]}',
                    encoding="utf-8",
                )

                with self.assertRaises(ValueError) as caught:
                    plan_claude_migration(source, root / "destination")
                self.assertIn(name, str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_settings_reject_duplicate_keys_without_values(self) -> None:
        payloads = (
            '{"parent": "safe", "parent": "do-not-leak"}',
            '{"env": {"SAFE": "safe", "SAFE": "do-not-leak"}}',
            '{"apiKeyHelper": "safe", "apiKeyHelper": "do-not-leak"}',
        )
        for payload in payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = self.make_source(root)
                (source / "settings.json").write_text(payload, encoding="utf-8")

                with self.assertRaises(ValueError) as caught:
                    plan_claude_migration(source, root / "destination")
                self.assertIn("duplicate", str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_source_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            plan_claude_migration(Path("relative"), Path("/tmp/destination"))

    def test_source_must_not_be_a_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real_source = self.make_source(root)
            source = root / "source-link"
            source.symlink_to(real_source, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                plan_claude_migration(source, root / "destination")

    def test_source_written_path_must_equal_its_strict_resolution(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            noncanonical = source / "hooks" / ".."

            with self.assertRaisesRegex(ValueError, "canonical"):
                plan_claude_migration(noncanonical, root / "destination")

    def test_allowlisted_symlink_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            (source / "hooks/run.sh").unlink()
            (source / "hooks/run.sh").symlink_to(source / "CLAUDE.md")

            with self.assertRaisesRegex(ValueError, "symlink"):
                plan_claude_migration(source, root / "destination")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_allowlisted_special_file_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            os.mkfifo(source / "hooks/pipe")

            with self.assertRaisesRegex(ValueError, "regular file or directory"):
                plan_claude_migration(source, root / "destination")

    def test_allowlisted_path_resolving_outside_source_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            outside = root / "outside"
            outside.write_text("outside", encoding="utf-8")
            hooks = source / "hooks"
            original_iterdir = Path.iterdir

            def escaping_iterdir(path: Path):
                if path == hooks:
                    return iter((hooks / ".." / ".." / "outside",))
                return original_iterdir(path)

            with patch.object(Path, "iterdir", escaping_iterdir):
                with self.assertRaisesRegex(ValueError, "escapes source"):
                    plan_claude_migration(source, root / "destination")

    def test_control_characters_in_allowlisted_names_are_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            (source / "hooks/forged\nSKIP denied credentials").write_text(
                "safe", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "control") as caught:
                plan_claude_migration(source, root / "destination")
            self.assertNotIn("SKIP denied credentials", str(caught.exception))

    def test_render_rejects_control_characters_in_manual_destination(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            plan = plan_claude_migration(source, root / "destination")
            plan = replace(plan, destination=root / "forged\nCOPY file secret")

            with self.assertRaisesRegex(ValueError, "control") as caught:
                render_migration_plan(plan)
            self.assertNotIn("COPY file secret", str(caught.exception))

    def test_existing_destination_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            destination.mkdir()

            with self.assertRaises(FileExistsError):
                plan_claude_migration(source, destination)


class PluginMigrationPlanningTest(MigrationTestCase):
    def make_plugin_source(self, root: Path) -> tuple[Path, Path]:
        source = self.make_source(root)
        selected = source / "plugins/cache/local-marketplace/issue-ops/1.2.3"
        unselected = source / "plugins/cache/local-marketplace/unselected/9.9.9"
        selected.mkdir(parents=True)
        unselected.mkdir(parents=True)
        (selected / "plugin.json").write_text(
            '{"name": "issue-ops"}\n', encoding="utf-8"
        )
        (unselected / "plugin.json").write_text(
            SECRET_MARKER, encoding="utf-8"
        )
        plugins = {
            "version": 2,
            "plugins": {
                "issue-ops@local-marketplace": [
                    {
                        "scope": "user",
                        "installPath": str(selected),
                        "version": "1.2.3",
                    }
                ],
                "unselected@local-marketplace": [
                    {
                        "scope": "user",
                        "installPath": str(unselected),
                        "version": "9.9.9",
                    }
                ],
            },
        }
        manifests = source / "plugins"
        (manifests / "installed_plugins.json").write_text(
            json.dumps(plugins), encoding="utf-8"
        )
        (manifests / "known_marketplaces.json").write_text(
            json.dumps(
                {
                    "local-marketplace": {
                        "source": {"source": "github", "repo": "safe/plugins"}
                    },
                    "unselected-marketplace": {"secret": SECRET_MARKER},
                }
            ),
            encoding="utf-8",
        )
        return source, selected

    def test_empty_plugin_selection_returns_original_plan(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            plan = plan_claude_migration(source, root / "destination")

            self.assertIs(add_plugin_entries(plan, ()), plan)

    def test_exact_plugin_selection_adds_only_referenced_cache_and_filtered_manifests(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            destination = root / "destination"
            plan = add_plugin_entries(
                plan_claude_migration(source, destination),
                (
                    "issue-ops@local-marketplace",
                    "issue-ops@local-marketplace",
                ),
            )

            selected_paths = tuple(
                entry.relative_path.as_posix() for entry in plan.entries
            )
            self.assertEqual(selected_paths, tuple(sorted(set(selected_paths))))
            self.assertIn(
                "plugins/cache/local-marketplace/issue-ops/1.2.3/plugin.json",
                selected_paths,
            )
            self.assertFalse(any("unselected" in path for path in selected_paths))
            generated = {
                item.relative_path.as_posix(): item.body
                for item in plan.generated_files
            }
            self.assertEqual(
                generated,
                {
                    "plugins/installed_plugins.json": (
                        b'{\n  "plugins": {\n'
                        b'    "issue-ops@local-marketplace": [\n'
                        b"      {\n"
                        b'        "installPath": "'
                        + str(
                            source
                            / "plugins/cache/local-marketplace/issue-ops/1.2.3"
                        ).encode()
                        + b'",\n        "scope": "user",\n'
                        b'        "version": "1.2.3"\n'
                        b"      }\n    ]\n  },\n  \"version\": 2\n}\n"
                    ),
                    "plugins/known_marketplaces.json": (
                        b'{\n  "local-marketplace": {\n'
                        b'    "source": {\n'
                        b'      "repo": "safe/plugins",\n'
                        b'      "source": "github"\n'
                        b"    }\n  }\n}\n"
                    ),
                },
            )
            self.assertNotIn(SECRET_MARKER, b"".join(generated.values()).decode())

            apply_claude_migration(plan)

            self.assertTrue(
                (
                    destination
                    / "plugins/cache/local-marketplace/issue-ops/1.2.3/plugin.json"
                ).is_file()
            )
            self.assertFalse(
                (destination / "plugins/cache/local-marketplace/unselected").exists()
            )

    def test_unknown_plugin_identifier_is_rejected_without_manifest_values(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError) as caught:
                add_plugin_entries(plan, ("missing@local-marketplace",))

            self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_installed_plugin_manifest_requires_exact_version_2_root_schema(self) -> None:
        malformed = (
            [],
            {"version": 1, "plugins": {}},
            {"version": 2},
            {"version": 2, "plugins": {}, "unexpected": SECRET_MARKER},
            {"version": 2, "plugins": []},
        )
        for payload in malformed:
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source, _ = self.make_plugin_source(root)
                (source / "plugins/installed_plugins.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                plan = plan_claude_migration(source, root / "destination")

                with self.assertRaises(ValueError) as caught:
                    add_plugin_entries(plan, ("issue-ops@local-marketplace",))

                self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_installed_plugin_manifest_rejects_numeric_version_lookalike(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            manifest_path = source / "plugins/installed_plugins.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = 2.0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError):
                add_plugin_entries(plan, ("issue-ops@local-marketplace",))

    def test_selected_plugin_requires_one_well_formed_record(self) -> None:
        malformed_records = (
            [],
            [{}, {}],
            [SECRET_MARKER],
            [{"installPath": SECRET_MARKER, "version": "1.2.3"}],
            [{"installPath": "/safe", "version": 123}],
        )
        for records in malformed_records:
            with self.subTest(records=records), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source, _ = self.make_plugin_source(root)
                manifest_path = source / "plugins/installed_plugins.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["plugins"]["issue-ops@local-marketplace"] = records
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                plan = plan_claude_migration(source, root / "destination")

                with self.assertRaises(ValueError) as caught:
                    add_plugin_entries(plan, ("issue-ops@local-marketplace",))

                self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_selected_cache_path_must_be_canonical_and_inside_plugin_cache(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            manifest_path = source / "plugins/installed_plugins.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["plugins"]["issue-ops@local-marketplace"][0][
                "installPath"
            ] = str(root / SECRET_MARKER)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError) as caught:
                add_plugin_entries(plan, ("issue-ops@local-marketplace",))

            self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_traversing_cache_path_error_hides_manifest_value(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            manifest_path = source / "plugins/installed_plugins.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["plugins"]["issue-ops@local-marketplace"][0][
                "installPath"
            ] = str(
                source
                / "plugins/cache"
                / SECRET_MARKER
                / ".."
                / "local-marketplace/issue-ops/1.2.3"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError) as caught:
                add_plugin_entries(plan, ("issue-ops@local-marketplace",))

            self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_selected_cache_path_must_not_contain_symlink_components(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, selected = self.make_plugin_source(root)
            outside = root / "outside"
            shutil.copytree(selected, outside / "1.2.3")
            shutil.rmtree(source / "plugins/cache/local-marketplace/issue-ops")
            (source / "plugins/cache/local-marketplace/issue-ops").symlink_to(
                outside, target_is_directory=True
            )
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError):
                add_plugin_entries(plan, ("issue-ops@local-marketplace",))

    def test_selected_plugin_requires_well_formed_marketplace_metadata(self) -> None:
        malformed = (
            [],
            {"different-marketplace": {"secret": SECRET_MARKER}},
            {"local-marketplace": SECRET_MARKER},
        )
        for payload in malformed:
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source, _ = self.make_plugin_source(root)
                (source / "plugins/known_marketplaces.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                plan = plan_claude_migration(source, root / "destination")

                with self.assertRaises(ValueError) as caught:
                    add_plugin_entries(plan, ("issue-ops@local-marketplace",))

                self.assertNotIn(SECRET_MARKER, str(caught.exception))

    def test_plugin_metadata_rejects_nonstandard_json_constants(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, _ = self.make_plugin_source(root)
            (source / "plugins/known_marketplaces.json").write_text(
                '{"local-marketplace": {"source": NaN}}', encoding="utf-8"
            )
            plan = plan_claude_migration(source, root / "destination")

            with self.assertRaises(ValueError):
                add_plugin_entries(plan, ("issue-ops@local-marketplace",))


class MigrationApplyTest(MigrationTestCase):
    def test_apply_uses_private_modes_and_preserves_source(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            source_bytes = (source / "hooks/run.sh").read_bytes()
            source_mode = stat.S_IMODE((source / "hooks/run.sh").stat().st_mode)
            plan = plan_claude_migration(source, destination)
            plan = replace(
                plan,
                generated_files=(
                    GeneratedFile(Path("generated/config.json"), b"{}\n"),
                    GeneratedFile(Path("generated/run.sh"), b"#!/bin/sh\n", True),
                ),
            )

            result = apply_claude_migration(plan)

            self.assertEqual(result, destination)
            for path in (
                destination,
                destination / "hooks",
                destination / "skills",
                destination / "skills/demo",
                destination / "generated",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((destination / "CLAUDE.md").stat().st_mode), 0o600
            )
            self.assertEqual(
                stat.S_IMODE((destination / "hooks/run.sh").stat().st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE((destination / "generated/config.json").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((destination / "generated/run.sh").stat().st_mode),
                0o700,
            )
            self.assertEqual((source / "hooks/run.sh").read_bytes(), source_bytes)
            self.assertEqual(
                stat.S_IMODE((source / "hooks/run.sh").stat().st_mode), source_mode
            )
            self.assert_no_stage(destination)

    def test_apply_leaves_destination_absent_when_source_file_becomes_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            (source / "CLAUDE.md").unlink()
            (source / "CLAUDE.md").symlink_to(source / "settings.json")

            with self.assertRaisesRegex(ValueError, "symlink"):
                apply_claude_migration(plan)
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_apply_opens_planned_files_nonblocking_before_rejecting_fifo(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            (source / "CLAUDE.md").unlink()
            os.mkfifo(source / "CLAUDE.md")
            original_open = os.open
            saw_nonblocking = False

            def guarded_open(path, flags, *args, **kwargs):
                nonlocal saw_nonblocking
                if path == "CLAUDE.md" and kwargs.get("dir_fd") is not None:
                    if not flags & os.O_NONBLOCK:
                        raise AssertionError("planned file open would block on FIFO")
                    saw_nonblocking = True
                return original_open(path, flags, *args, **kwargs)

            with patch("agent_container.migration.os.open", side_effect=guarded_open):
                with self.assertRaisesRegex(ValueError, "changed type"):
                    apply_claude_migration(plan)
            self.assertTrue(saw_nonblocking)
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_apply_rejects_settings_that_become_sensitive_after_planning(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            (source / "settings.json").write_text(
                '{"env": {"ACCESS_TOKEN": "do-not-leak"}}', encoding="utf-8"
            )

            with self.assertRaises(ValueError) as caught:
                apply_claude_migration(plan)
            self.assertNotIn("do-not-leak", str(caught.exception))
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_apply_copies_from_same_descriptor_when_source_name_becomes_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            outside = root / "outside"
            outside.write_text(SECRET_MARKER, encoding="utf-8")
            plan = plan_claude_migration(source, destination)
            original_copyfileobj = shutil.copyfileobj
            mutated = False

            def mutate_during_copy(source_stream, destination_stream, *args, **kwargs):
                nonlocal mutated
                if not mutated:
                    claude_file = source / "CLAUDE.md"
                    claude_file.unlink()
                    claude_file.symlink_to(outside)
                    mutated = True
                return original_copyfileobj(
                    source_stream, destination_stream, *args, **kwargs
                )

            with patch(
                "agent_container.migration.shutil.copyfileobj",
                side_effect=mutate_during_copy,
            ):
                apply_claude_migration(plan)

            self.assertTrue(mutated)
            self.assertEqual(
                (destination / "CLAUDE.md").read_text(encoding="utf-8"),
                "safe instructions\n",
            )
            self.assertNotIn(
                SECRET_MARKER,
                (destination / "CLAUDE.md").read_text(encoding="utf-8"),
            )

    def test_apply_creates_outer_stage_without_reopening_parent_path(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)

            with patch(
                "tempfile.mkdtemp",
                side_effect=AssertionError("destination parent pathname reopened"),
            ):
                apply_claude_migration(plan)

            self.assertTrue(destination.is_dir())
            self.assert_no_stage(destination)

    def test_apply_publishes_held_payload_when_outer_stage_name_is_replaced(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            original_copyfileobj = shutil.copyfileobj
            replacement: Path | None = None

            def replace_outer_during_copy(source_stream, destination_stream):
                nonlocal replacement
                if replacement is None:
                    outer = next(
                        destination.parent.glob(f".{destination.name}.migrate-*")
                    )
                    outer.rename(root / "original-outer-moved")
                    outer.mkdir()
                    replacement = outer
                    (outer / "payload").mkdir()
                    (outer / "payload/CLAUDE.md").write_text(
                        SECRET_MARKER, encoding="utf-8"
                    )
                return original_copyfileobj(source_stream, destination_stream)

            with patch(
                "agent_container.migration.shutil.copyfileobj",
                side_effect=replace_outer_during_copy,
            ):
                apply_claude_migration(plan)

            self.assertEqual(
                (destination / "CLAUDE.md").read_text(encoding="utf-8"),
                "safe instructions\n",
            )
            self.assertNotIn(
                SECRET_MARKER,
                (destination / "CLAUDE.md").read_text(encoding="utf-8"),
            )
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertEqual(
                (replacement / "payload/CLAUDE.md").read_text(encoding="utf-8"),
                SECRET_MARKER,
            )

    def test_apply_leaves_late_existing_destination_unchanged(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            destination.mkdir()
            marker = destination / "marker"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                apply_claude_migration(plan)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assert_no_stage(destination)

    def test_apply_does_not_replace_destination_created_at_publish(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            from agent_container import migration

            original_validate = migration._validate_destination
            validations = 0

            def create_destination_after_final_check(path: Path) -> None:
                nonlocal validations
                original_validate(path)
                validations += 1
                if validations == 2:
                    destination.mkdir()

            with patch.object(
                migration,
                "_validate_destination",
                side_effect=create_destination_after_final_check,
            ):
                with self.assertRaises(FileExistsError):
                    apply_claude_migration(plan)
            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])
            self.assert_no_stage(destination)

    def test_apply_cleans_stage_after_copy_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)

            with patch(
                "agent_container.migration.shutil.copyfile",
                side_effect=OSError("DO-NOT-PRINT-COPY-FAILURE"),
            ), patch(
                "agent_container.migration.shutil.copyfileobj",
                side_effect=OSError("DO-NOT-PRINT-COPY-FAILURE"),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    apply_claude_migration(plan)
            self.assertEqual(str(caught.exception), "migration filesystem operation failed")
            self.assertNotIn("DO-NOT-PRINT-COPY-FAILURE", str(caught.exception))
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_apply_sanitizes_unexpected_file_exists_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)

            with patch(
                "agent_container.migration.shutil.copyfileobj",
                side_effect=FileExistsError("DO-NOT-PRINT-FILE-EXISTS"),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    apply_claude_migration(plan)
            self.assertEqual(str(caught.exception), "migration filesystem operation failed")
            self.assertNotIn("DO-NOT-PRINT-FILE-EXISTS", str(caught.exception))
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_cleanup_does_not_delete_a_replacement_stage_directory(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            replacement: Path | None = None

            def replace_stage_and_fail(*args, **kwargs):
                nonlocal replacement
                stage = next(
                    destination.parent.glob(f".{destination.name}.migrate-*")
                )
                stage.rename(root / "original-stage-moved")
                stage.mkdir()
                replacement = stage
                (stage / "replacement-marker").write_text("keep", encoding="utf-8")
                raise OSError("DO-NOT-PRINT-CLEANUP-FAILURE")

            with patch(
                "agent_container.migration.shutil.copyfile",
                side_effect=replace_stage_and_fail,
            ), patch(
                "agent_container.migration.shutil.copyfileobj",
                side_effect=replace_stage_and_fail,
            ):
                with self.assertRaises(RuntimeError):
                    apply_claude_migration(plan)

            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertEqual(
                (replacement / "replacement-marker").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
