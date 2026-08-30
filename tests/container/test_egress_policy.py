from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from agent_container.egress_policy import EgressPolicy
from agent_container.egress_policy import add_egress_domain
from agent_container.egress_policy import disable_egress_policy
from agent_container.egress_policy import enable_egress_policy
from agent_container.egress_policy import load_egress_policy
from agent_container.egress_policy import remove_egress_domain
from agent_container.egress_policy import validate_domain


class EgressDomainValidationTest(unittest.TestCase):
    def test_accepts_exact_lowercase_ascii_dns_name(self) -> None:
        self.assertEqual(validate_domain("files.pythonhosted.org"), "files.pythonhosted.org")
        self.assertEqual(validate_domain("a-b.example"), "a-b.example")

    def test_rejects_noncanonical_and_unsafe_destinations(self) -> None:
        denied = (
            "",
            "EXAMPLE.com",
            "example.com.",
            "*.example.com",
            "127.0.0.1",
            "[::1]",
            "localhost",
            "service.local",
            "service.internal",
            "service.home",
            "service.arpa",
            "example.com:443",
            "https://example.com",
            "user@example.com",
            "example.com/path",
            "xn--e1afmkfd.xn--p1ai",
            "éxample.com",
            "under_score.example",
            "-bad.example",
            "bad-.example",
            "two..example",
            f"{'a' * 64}.example",
            f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}.example",
            "example.com\nnext",
        )
        for value in denied:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_domain(value)
        for value in (None, True, 443, b"example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_domain(value)


class EgressPolicyPersistenceTest(unittest.TestCase):
    def _private_project(self, temp: str) -> tuple[Path, Path]:
        project = Path(temp) / "project"
        project.mkdir(mode=0o700)
        return project, project / "egress.json"

    def _write(self, path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def test_loads_only_exact_sorted_version_one_schema(self) -> None:
        with TemporaryDirectory() as temp:
            _project, path = self._private_project(temp)
            self._write(
                path,
                {
                    "version": 1,
                    "mode": "allowlist",
                    "additional_domains": ["files.pythonhosted.org", "pypi.org"],
                },
            )
            self.assertEqual(
                load_egress_policy(path),
                EgressPolicy(1, "allowlist", ("files.pythonhosted.org", "pypi.org")),
            )

    def test_rejects_unknown_duplicate_unsorted_and_unsafe_files(self) -> None:
        invalid = (
            {"version": 1, "mode": "allowlist"},
            {"version": 2, "mode": "allowlist", "additional_domains": []},
            {"version": 1, "mode": "open", "additional_domains": []},
            {"version": 1, "mode": "allowlist", "additional_domains": ["pypi.org", "files.pythonhosted.org"]},
            {"version": 1, "mode": "allowlist", "additional_domains": ["pypi.org", "pypi.org"]},
            {"version": 1, "mode": "allowlist", "additional_domains": [], "extra": True},
        )
        for index, payload in enumerate(invalid):
            with self.subTest(index=index), TemporaryDirectory() as temp:
                _project, path = self._private_project(temp)
                self._write(path, payload)
                with self.assertRaises(ValueError):
                    load_egress_policy(path)

        with TemporaryDirectory() as temp:
            project, path = self._private_project(temp)
            target = project / "target"
            self._write(target, {"version": 1, "mode": "allowlist", "additional_domains": []})
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_egress_policy(path)

    def test_enable_add_remove_disable_round_trip(self) -> None:
        with TemporaryDirectory() as temp:
            _project, path = self._private_project(temp)
            self.assertEqual(enable_egress_policy(path), EgressPolicy(1, "allowlist", ()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                add_egress_domain(path, "pypi.org"),
                EgressPolicy(1, "allowlist", ("pypi.org",)),
            )
            self.assertEqual(
                add_egress_domain(path, "files.pythonhosted.org"),
                EgressPolicy(1, "allowlist", ("files.pythonhosted.org", "pypi.org")),
            )
            with self.assertRaises(ValueError):
                add_egress_domain(path, "pypi.org")
            self.assertEqual(
                remove_egress_domain(path, "pypi.org"),
                EgressPolicy(1, "allowlist", ("files.pythonhosted.org",)),
            )
            with self.assertRaises(ValueError):
                remove_egress_domain(path, "pypi.org")
            disable_egress_policy(path)
            self.assertFalse(path.exists())

    def test_failed_replace_preserves_prior_bytes_and_removes_temp(self) -> None:
        with TemporaryDirectory() as temp:
            project, path = self._private_project(temp)
            enable_egress_policy(path)
            before = path.read_bytes()
            with patch("agent_container.egress_policy.os.replace", side_effect=OSError("marker")):
                with self.assertRaises(OSError):
                    add_egress_domain(path, "pypi.org")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([entry.name for entry in project.iterdir()], ["egress.json"])

    def test_rejects_broad_parent_and_file_modes(self) -> None:
        with TemporaryDirectory() as temp:
            project, path = self._private_project(temp)
            project.chmod(0o750)
            with self.assertRaises(PermissionError):
                enable_egress_policy(path)
        with TemporaryDirectory() as temp:
            _project, path = self._private_project(temp)
            self._write(path, {"version": 1, "mode": "allowlist", "additional_domains": []})
            path.chmod(0o640)
            with self.assertRaises(PermissionError):
                load_egress_policy(path)


if __name__ == "__main__":
    unittest.main()
