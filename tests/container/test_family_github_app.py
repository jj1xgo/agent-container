from base64 import urlsafe_b64decode
from copy import deepcopy
from datetime import datetime, timezone
from email.message import Message
import errno
import fcntl
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request
import urllib.response

import agent_container.family_github_app as family_github_app
import agent_container.github_app as development_github_app
from agent_container.family_github_app import FamilyAppMetadata
from agent_container.family_github_app import FamilyInstallationTokenProvider
from agent_container.family_github_app import family_repository_transport
from agent_container.family_github_app import verify_family_repository
from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.state import Repository


API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 1_048_576


def redirecting_opener_factory(
    requests: list[tuple[str, str, str | None]],
):  # type: ignore[no-untyped-def]
    real_build_opener = urllib.request.build_opener

    class RedirectingHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):  # type: ignore[no-untyped-def]
            requests.append(
                (
                    request.get_method(),
                    request.full_url,
                    request.get_header("Authorization"),
                )
            )
            headers = Message()
            status = 200 if request.full_url.endswith("/redirect-target") else 302
            if status == 302:
                headers["Location"] = "https://api.github.com/redirect-target"
            response = urllib.response.addinfourl(
                BytesIO(b""), headers, request.full_url, status
            )
            response.msg = "OK" if status == 200 else "Found"
            return response

    def factory(*handlers):  # type: ignore[no-untyped-def]
        return real_build_opener(*handlers, RedirectingHTTPSHandler())

    return factory


def decode_segment(value: str) -> dict[str, object]:
    padding = "=" * (-len(value) % 4)
    return json.loads(urlsafe_b64decode(value + padding))


class FakeSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int]] = []

    def sign(self, content: bytes, private_key: int) -> bytes:
        self.calls.append((content, private_key))
        return b"family-binary-signature"


def token_response() -> HttpResponse:
    return HttpResponse(
        status=201,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=json.dumps(
            {
                "token": "family-installation-token-marker",
                "expires_at": "2027-01-15T09:00:00Z",
                "permissions": {"issues": "write", "metadata": "read"},
                "repository_selection": "selected",
            }
        ).encode("ascii"),
    )


def repository_fixture() -> dict[str, object]:
    """Complete documented installation-repository shape with fixed family values."""
    return {
        "id": 42,
        "node_id": "R_kgDOFamilyRoadmap",
        "name": "roadmap",
        "full_name": "family/roadmap",
        "owner": {
            "login": "family",
            "id": 7,
            "node_id": "O_kgDOFamily",
            "avatar_url": "https://avatars.githubusercontent.com/u/7?v=4",
            "gravatar_id": "",
            "url": "https://api.github.com/users/family",
            "html_url": "https://github.com/family",
            "followers_url": "https://api.github.com/users/family/followers",
            "following_url": "https://api.github.com/users/family/following{/other_user}",
            "gists_url": "https://api.github.com/users/family/gists{/gist_id}",
            "starred_url": "https://api.github.com/users/family/starred{/owner}{/repo}",
            "subscriptions_url": "https://api.github.com/users/family/subscriptions",
            "organizations_url": "https://api.github.com/users/family/orgs",
            "repos_url": "https://api.github.com/users/family/repos",
            "events_url": "https://api.github.com/users/family/events{/privacy}",
            "received_events_url": "https://api.github.com/users/family/received_events",
            "type": "Organization",
            "site_admin": False,
        },
        "private": True,
        "html_url": "https://github.com/family/roadmap",
        "description": "Family planning",
        "fork": False,
        "url": "https://api.github.com/repos/family/roadmap",
        "archive_url": "https://api.github.com/repos/family/roadmap/{archive_format}{/ref}",
        "assignees_url": "https://api.github.com/repos/family/roadmap/assignees{/user}",
        "blobs_url": "https://api.github.com/repos/family/roadmap/git/blobs{/sha}",
        "branches_url": "https://api.github.com/repos/family/roadmap/branches{/branch}",
        "collaborators_url": "https://api.github.com/repos/family/roadmap/collaborators{/collaborator}",
        "comments_url": "https://api.github.com/repos/family/roadmap/comments{/number}",
        "commits_url": "https://api.github.com/repos/family/roadmap/commits{/sha}",
        "compare_url": "https://api.github.com/repos/family/roadmap/compare/{base}...{head}",
        "contents_url": "https://api.github.com/repos/family/roadmap/contents/{+path}",
        "contributors_url": "https://api.github.com/repos/family/roadmap/contributors",
        "deployments_url": "https://api.github.com/repos/family/roadmap/deployments",
        "downloads_url": "https://api.github.com/repos/family/roadmap/downloads",
        "events_url": "https://api.github.com/repos/family/roadmap/events",
        "forks_url": "https://api.github.com/repos/family/roadmap/forks",
        "git_commits_url": "https://api.github.com/repos/family/roadmap/git/commits{/sha}",
        "git_refs_url": "https://api.github.com/repos/family/roadmap/git/refs{/sha}",
        "git_tags_url": "https://api.github.com/repos/family/roadmap/git/tags{/sha}",
        "git_url": "git://github.com/family/roadmap.git",
        "issue_comment_url": "https://api.github.com/repos/family/roadmap/issues/comments{/number}",
        "issue_events_url": "https://api.github.com/repos/family/roadmap/issues/events{/number}",
        "issues_url": "https://api.github.com/repos/family/roadmap/issues{/number}",
        "keys_url": "https://api.github.com/repos/family/roadmap/keys{/key_id}",
        "labels_url": "https://api.github.com/repos/family/roadmap/labels{/name}",
        "languages_url": "https://api.github.com/repos/family/roadmap/languages",
        "merges_url": "https://api.github.com/repos/family/roadmap/merges",
        "milestones_url": "https://api.github.com/repos/family/roadmap/milestones{/number}",
        "notifications_url": "https://api.github.com/repos/family/roadmap/notifications{?since,all,participating}",
        "pulls_url": "https://api.github.com/repos/family/roadmap/pulls{/number}",
        "releases_url": "https://api.github.com/repos/family/roadmap/releases{/id}",
        "ssh_url": "git@github.com:family/roadmap.git",
        "stargazers_url": "https://api.github.com/repos/family/roadmap/stargazers",
        "statuses_url": "https://api.github.com/repos/family/roadmap/statuses/{sha}",
        "subscribers_url": "https://api.github.com/repos/family/roadmap/subscribers",
        "subscription_url": "https://api.github.com/repos/family/roadmap/subscription",
        "tags_url": "https://api.github.com/repos/family/roadmap/tags",
        "teams_url": "https://api.github.com/repos/family/roadmap/teams",
        "trees_url": "https://api.github.com/repos/family/roadmap/git/trees{/sha}",
        "clone_url": "https://github.com/family/roadmap.git",
        "mirror_url": None,
        "hooks_url": "https://api.github.com/repos/family/roadmap/hooks",
        "svn_url": "https://github.com/family/roadmap",
        "homepage": None,
        "language": "Python",
        "forks_count": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "size": 17,
        "default_branch": "main",
        "open_issues_count": 3,
        "is_template": False,
        "topics": ["family", "planning"],
        "has_issues": True,
        "has_projects": False,
        "has_wiki": False,
        "has_pages": False,
        "has_downloads": True,
        "archived": False,
        "disabled": False,
        "visibility": "private",
        "pushed_at": "2027-01-15T07:45:00Z",
        "created_at": "2026-01-15T08:00:00Z",
        "updated_at": "2027-01-15T07:45:00Z",
        "allow_rebase_merge": True,
        "template_repository": None,
        "temp_clone_token": "fixture-token-not-used-by-family-code",
        "allow_squash_merge": True,
        "allow_auto_merge": False,
        "delete_branch_on_merge": True,
        "allow_merge_commit": True,
        "subscribers_count": 1,
        "network_count": 0,
        "license": None,
        "forks": 0,
        "open_issues": 3,
        "watchers": 0,
        "custom_properties": {"audience": "family"},
    }


def inventory_response(
    repositories: list[dict[str, object]] | None = None,
    *,
    total_count: int | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    selected = [repository_fixture()] if repositories is None else repositories
    count = len(selected) if total_count is None else total_count
    return HttpResponse(
        status=200,
        headers={"Content-Type": "application/json; charset=utf-8"}
        if headers is None
        else headers,
        body=json.dumps(
            {"total_count": count, "repositories": selected}
        ).encode("utf-8"),
    )


class FamilyGitHubAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = FamilyStateLayout(self.root, "demo")
        self.layout.family_root.mkdir(mode=0o700)
        self.layout.family_app_file.write_text(
            json.dumps(
                {"client_id": "Iv1familyabcdefgh", "installation_id": 123}
            ),
            encoding="ascii",
        )
        self.layout.family_private_key_file.write_text(
            "family-private-key-marker", encoding="ascii"
        )
        self.layout.family_app_file.chmod(0o600)
        self.layout.family_private_key_file.chmod(0o600)

    def metadata(self) -> FamilyAppMetadata:
        return FamilyAppMetadata.load(self.layout)

    def provider(self, response: HttpResponse | None = None):  # type: ignore[no-untyped-def]
        return FamilyInstallationTokenProvider(
            self.layout,
            signer=FakeSigner(),
            transport=lambda *_: token_response() if response is None else response,
            clock=lambda: 1_800_000_000,
        )

    def test_loads_only_the_exact_family_metadata_schema_and_private_key_path(
        self,
    ) -> None:
        metadata = self.metadata()

        self.assertEqual(metadata.client_id, "Iv1familyabcdefgh")
        self.assertEqual(metadata.installation_id, 123)
        self.assertEqual(metadata.private_key, self.layout.family_private_key_file)
        self.assertNotIn("repository_id", FamilyAppMetadata.__dataclass_fields__)
        self.assertNotIn("family-private-key-marker", repr(metadata))

    def test_rejects_duplicate_unknown_missing_or_invalid_metadata(self) -> None:
        cases = (
            b'{"client_id":"Iv1familyabcdefgh","client_id":"Iv1otherabcdefgh","installation_id":123}',
            b'{"client_id":"Iv1familyabcdefgh","installation_id":123,"repository_id":42}',
            b'{"client_id":"Iv1familyabcdefgh"}',
            b'{"client_id":"bad id","installation_id":123}',
            b'{"client_id":"Iv1familyabcdefgh","installation_id":true}',
            b'{"client_id":"Iv1familyabcdefgh","installation_id":0}',
            b'{"client_id":"Iv1familyabcdefgh","installation_id":NaN}',
            b'{"client_id":"Iv1familyabcdefgh","installation_id":123',
            b'\xff',
        )
        for body in cases:
            with self.subTest(body=body):
                self.layout.family_app_file.write_bytes(body)
                self.layout.family_app_file.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
                    self.metadata()

    def test_rejects_non_private_or_non_exact_family_files(self) -> None:
        self.layout.family_app_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.metadata()
        self.layout.family_app_file.chmod(0o600)

        self.layout.family_private_key_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.metadata()
        self.layout.family_private_key_file.chmod(0o600)

        self.layout.family_root.chmod(0o755)
        with self.assertRaises(PermissionError):
            self.metadata()
        self.layout.family_root.chmod(0o700)

        self.root.chmod(0o755)
        with self.assertRaises(PermissionError):
            self.metadata()
        self.root.chmod(0o700)

        hard_link = self.root / "app-hard-link.json"
        os.link(self.layout.family_app_file, hard_link)
        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            self.metadata()
        hard_link.unlink()

        with mock.patch(
            "agent_container.family_github_app.os.getuid",
            return_value=os.getuid() + 1,
        ):
            with self.assertRaises(PermissionError):
                self.metadata()

        key_target = self.root / "elsewhere.pem"
        key_target.write_text("family-private-key-marker", encoding="ascii")
        key_target.chmod(0o600)
        self.layout.family_private_key_file.unlink()
        self.layout.family_private_key_file.symlink_to(key_target)
        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            self.metadata()

    def test_rejects_family_layout_with_symlinked_ancestor(self) -> None:
        real_family = self.root / "real-family"
        real_family.mkdir(mode=0o700)
        for source, name in (
            (self.layout.family_app_file, "app.json"),
            (self.layout.family_private_key_file, "private-key.pem"),
        ):
            (real_family / name).write_bytes(source.read_bytes())
            (real_family / name).chmod(0o600)
            source.unlink()
        self.layout.family_root.rmdir()
        self.layout.family_root.symlink_to(real_family, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            self.metadata()

    def test_rejects_oversized_flat_or_deep_metadata_before_json_parse(self) -> None:
        valid = json.dumps(
            {"client_id": "Iv1familyabcdefgh", "installation_id": 123}
        ).encode("ascii")
        cases = (
            valid + b" " * (4097 - len(valid)),
            (b'{"nested":' * 455) + b"0" + (b"}" * 455),
        )
        for body in cases:
            with self.subTest(size=len(body)):
                self.layout.family_app_file.write_bytes(body)
                self.layout.family_app_file.chmod(0o600)
                with mock.patch(
                    "agent_container.family_github_app.json.loads",
                    side_effect=AssertionError("oversized metadata reached JSON"),
                ) as loads:
                    with self.assertRaises((ValueError, AssertionError)):
                        self.metadata()
                loads.assert_not_called()

    def test_rejects_oversized_private_key_before_signing(self) -> None:
        self.layout.family_private_key_file.write_bytes(b"K" * 65_537)
        self.layout.family_private_key_file.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            self.metadata()

    def test_descriptor_reader_completes_partial_metadata_and_key_reads(self) -> None:
        real_read = os.read

        def partial_read(descriptor: int, maximum: int) -> bytes:
            return real_read(descriptor, min(maximum, 3))

        with mock.patch(
            "agent_container.family_github_app.os.read", side_effect=partial_read
        ):
            metadata = self.metadata()

        self.assertEqual(metadata.client_id, "Iv1familyabcdefgh")
        self.assertEqual(metadata.installation_id, 123)

    @mock.patch("agent_container.family_github_app.subprocess.run")
    def test_family_openssl_signer_uses_only_the_pinned_fd_and_fixed_process(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, b"signature", b"")
        with family_github_app._sealed_key_snapshot(
            b"family-private-key-marker"
        ) as descriptor:
            signature = family_github_app.FamilyOpenSSLSigner().sign(
                b"content", descriptor
            )

        self.assertEqual(signature, b"signature")
        call = run.call_args
        self.assertEqual(
            call.args[0],
            (
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-sign",
                f"/proc/self/fd/{descriptor}",
            ),
        )
        self.assertEqual(call.kwargs["pass_fds"], (descriptor,))
        self.assertEqual(call.kwargs["input"], b"content")
        self.assertEqual(
            call.kwargs["env"],
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "OPENSSL_CONF": "/dev/null",
            },
        )
        self.assertTrue(call.kwargs["capture_output"])
        self.assertFalse(call.kwargs["check"])

    def test_rejects_app_entry_swap_before_descriptor_open(self) -> None:
        replacement = self.layout.family_root / ".replacement-app.json"
        replacement.write_bytes(self.layout.family_app_file.read_bytes())
        replacement.chmod(0o600)
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal swapped
            if path == "app.json" and not swapped:
                swapped = True
                os.replace(replacement, self.layout.family_app_file)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch(
            "agent_container.family_github_app.os.open", side_effect=swap_before_open
        ):
            with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
                self.metadata()

        self.assertTrue(swapped)

    def test_rejects_app_entry_swap_during_descriptor_read(self) -> None:
        replacement = self.layout.family_root / ".replacement-app.json"
        replacement.write_bytes(self.layout.family_app_file.read_bytes())
        replacement.chmod(0o600)
        real_read = os.read
        swapped = False

        def swap_after_read(descriptor: int, maximum: int) -> bytes:
            nonlocal swapped
            chunk = real_read(descriptor, maximum)
            if chunk and not swapped:
                swapped = True
                os.replace(replacement, self.layout.family_app_file)
            return chunk

        with mock.patch(
            "agent_container.family_github_app.os.read", side_effect=swap_after_read
        ):
            with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
                self.metadata()

        self.assertTrue(swapped)

    def test_rejects_key_entry_swap_during_descriptor_read(self) -> None:
        replacement = self.layout.family_root / ".replacement-private-key.pem"
        replacement.write_bytes(self.layout.family_private_key_file.read_bytes())
        replacement.chmod(0o600)
        real_read = os.read
        swapped = False

        def swap_key_after_read(descriptor: int, maximum: int) -> bytes:
            nonlocal swapped
            chunk = real_read(descriptor, maximum)
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if target.endswith("/private-key.pem") and chunk and not swapped:
                swapped = True
                os.replace(replacement, self.layout.family_private_key_file)
            return chunk

        with mock.patch(
            "agent_container.family_github_app.os.read",
            side_effect=swap_key_after_read,
        ):
            with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
                self.metadata()

        self.assertTrue(swapped)

    def test_filesystem_errors_are_fixed_and_do_not_echo_paths(self) -> None:
        marker = f"filesystem-secret:{self.layout.family_private_key_file}"
        with mock.patch(
            "agent_container.family_github_app.os.open",
            side_effect=OSError(marker),
        ):
            with self.assertRaises(ValueError) as raised:
                self.metadata()

        self.assertEqual(str(raised.exception), "family GitHub App metadata is invalid")
        self.assertNotIn(marker, repr(raised.exception))

    def test_provider_rejects_replaced_app_or_key_before_side_effects(self) -> None:
        for replaced in ("app", "key"):
            with self.subTest(replaced=replaced):
                signer = FakeSigner()
                transport_calls: list[object] = []
                provider = FamilyInstallationTokenProvider(
                    self.layout,
                    signer=signer,
                    transport=lambda *call: (
                        transport_calls.append(call) or token_response()
                    ),
                    clock=lambda: 1_800_000_000,
                )
                target = (
                    self.layout.family_app_file
                    if replaced == "app"
                    else self.layout.family_private_key_file
                )
                replacement = self.layout.family_root / f".{replaced}-replacement"
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
                os.replace(replacement, target)

                with self.assertRaisesRegex(
                    ValueError, "family GitHub App metadata"
                ):
                    provider.get()

                self.assertEqual(signer.calls, [])
                self.assertEqual(transport_calls, [])

                if replaced == "app":
                    self.layout.family_app_file.write_text(
                        json.dumps(
                            {
                                "client_id": "Iv1familyabcdefgh",
                                "installation_id": 123,
                            }
                        ),
                        encoding="ascii",
                    )
                else:
                    self.layout.family_private_key_file.write_text(
                        "family-private-key-marker", encoding="ascii"
                    )
                target.chmod(0o600)

    def test_key_fd_stays_pinned_and_swap_during_sign_fails_before_network(
        self,
    ) -> None:
        transport_calls: list[object] = []

        class SwappingSigner:
            def __init__(self) -> None:
                self.calls = 0
                self.received_descriptor = False
                self.pinned_body = b""

            def sign(self, content, private_key):  # type: ignore[no-untyped-def]
                self.calls += 1
                self.received_descriptor = isinstance(private_key, int)
                if self.received_descriptor:
                    self.pinned_body = os.pread(private_key, 4096, 0)
                replacement = self.layout.family_root / ".key-during-sign"
                replacement.write_text("replacement-key-marker", encoding="ascii")
                replacement.chmod(0o600)
                os.replace(replacement, self.layout.family_private_key_file)
                if self.received_descriptor:
                    self.assertEqual(
                        os.pread(private_key, 4096, 0), self.pinned_body
                    )
                return b"family-binary-signature"

        signer = SwappingSigner()
        signer.layout = self.layout
        signer.assertEqual = self.assertEqual
        provider = FamilyInstallationTokenProvider(
            self.layout,
            signer=signer,  # type: ignore[arg-type]
            transport=lambda *call: transport_calls.append(call) or token_response(),
            clock=lambda: 1_800_000_000,
        )

        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            provider.get()

        self.assertEqual(signer.calls, 1)
        self.assertTrue(signer.received_descriptor)
        self.assertEqual(signer.pinned_body, b"family-private-key-marker")
        self.assertEqual(transport_calls, [])

    def test_provider_sanitizes_descriptor_close_failure_before_network(self) -> None:
        signer = FakeSigner()
        transport_calls: list[object] = []
        provider = FamilyInstallationTokenProvider(
            self.layout,
            signer=signer,
            transport=lambda *call: transport_calls.append(call) or token_response(),
            clock=lambda: 1_800_000_000,
        )
        marker = "descriptor-close-secret-marker"
        real_close = os.close
        injected = False

        def fail_key_close(descriptor: int) -> None:
            nonlocal injected
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                target = ""
            real_close(descriptor)
            if target.endswith("/private-key.pem") and not injected:
                injected = True
                raise OSError(marker)

        with mock.patch(
            "agent_container.family_github_app.os.close", side_effect=fail_key_close
        ):
            with self.assertRaises(ValueError) as raised:
                provider.get()

        self.assertTrue(injected)
        self.assertEqual(str(raised.exception), "family GitHub App metadata is invalid")
        self.assertNotIn(marker, repr(raised.exception))
        self.assertEqual(transport_calls, [])

    def test_uses_a_distinct_family_identity_and_provider_boundary(self) -> None:
        self.assertIsNot(FamilyAppMetadata, development_github_app.GitHubAppMetadata)
        self.assertIsNot(
            FamilyInstallationTokenProvider,
            development_github_app.InstallationTokenProvider,
        )
        self.assertFalse(
            issubclass(FamilyAppMetadata, development_github_app.GitHubAppMetadata)
        )
        self.assertFalse(
            issubclass(
                FamilyInstallationTokenProvider,
                development_github_app.InstallationTokenProvider,
            )
        )
        with self.assertRaises(TypeError):
            FamilyAppMetadata(  # type: ignore[call-arg]
                "Iv1familyabcdefgh",
                123,
                self.layout.family_private_key_file,
            )

    def test_provider_rejects_cross_wired_or_forged_metadata_before_side_effects(
        self,
    ) -> None:
        development = development_github_app.GitHubAppMetadata(
            client_id="Iv1developmentabc",
            installation_id=999,
            repository_id=42,
            private_key=self.layout.family_private_key_file,
        )
        other_root = self.root / "other-state"
        other_root.mkdir(mode=0o700)
        other_layout = FamilyStateLayout(other_root, "demo")
        other_layout.family_root.mkdir(mode=0o700)
        other_layout.family_app_file.write_bytes(self.layout.family_app_file.read_bytes())
        other_layout.family_private_key_file.write_bytes(
            self.layout.family_private_key_file.read_bytes()
        )
        other_layout.family_app_file.chmod(0o600)
        other_layout.family_private_key_file.chmod(0o600)
        other_metadata = FamilyAppMetadata.load(other_layout)
        forged = object.__new__(FamilyAppMetadata)
        for name in FamilyAppMetadata.__dataclass_fields__:
            object.__setattr__(forged, name, getattr(other_metadata, name))

        for metadata in (development, other_metadata, forged):
            with self.subTest(metadata_type=type(metadata).__name__):
                signer = FakeSigner()
                transport_calls: list[object] = []
                with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
                    provider = FamilyInstallationTokenProvider(
                        metadata,  # type: ignore[arg-type]
                        signer=signer,
                        transport=lambda *call: (
                            transport_calls.append(call) or token_response()
                        ),
                        clock=lambda: 1_800_000_000,
                    )
                    provider.get()
                self.assertEqual(signer.calls, [])
                self.assertEqual(transport_calls, [])

    def test_provider_uses_an_independently_supplied_trusted_layout(self) -> None:
        signer = FakeSigner()
        calls: list[object] = []
        provider = FamilyInstallationTokenProvider(
            self.layout,
            signer=signer,
            transport=lambda *call: calls.append(call) or token_response(),
            clock=lambda: 1_800_000_000,
        )

        self.assertEqual(provider.get().token, "family-installation-token-marker")
        self.assertEqual(len(signer.calls), 1)
        self.assertEqual(len(calls), 1)

    def test_sealed_key_snapshot_blocks_all_same_inode_mutations_before_network(
        self,
    ) -> None:
        required_seals = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL
        )

        for mutation in ("truncate", "rewrite", "grow"):
            with self.subTest(mutation=mutation):
                self.layout.family_private_key_file.write_bytes(
                    b"family-private-key-marker"
                )
                self.layout.family_private_key_file.chmod(0o600)
                transport_calls: list[object] = []

                class MutatingSigner:
                    def __init__(inner_self) -> None:
                        inner_self.before = b""
                        inner_self.after = b""
                        inner_self.seals = 0
                        inner_self.snapshot_size = 0
                        inner_self.write_errno: int | None = None

                    def sign(inner_self, content, descriptor):  # type: ignore[no-untyped-def]
                        inner_self.before = os.pread(descriptor, 65_536, 0)
                        inner_self.snapshot_size = os.fstat(descriptor).st_size
                        inner_self.seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
                        try:
                            os.pwrite(descriptor, b"X", 0)
                        except OSError as error:
                            inner_self.write_errno = error.errno
                        if mutation == "truncate":
                            os.truncate(self.layout.family_private_key_file, 5)
                        elif mutation == "rewrite":
                            source = os.open(
                                self.layout.family_private_key_file, os.O_WRONLY
                            )
                            try:
                                os.pwrite(source, b"replacement-key-material", 0)
                            finally:
                                os.close(source)
                        else:
                            source = os.open(
                                self.layout.family_private_key_file, os.O_WRONLY
                            )
                            try:
                                os.lseek(source, 0, os.SEEK_END)
                                os.write(source, b"-grown")
                            finally:
                                os.close(source)
                        inner_self.after = os.pread(descriptor, 65_536, 0)
                        return b"family-binary-signature"

                signer = MutatingSigner()
                provider = FamilyInstallationTokenProvider(
                    self.layout,
                    signer=signer,  # type: ignore[arg-type]
                    transport=lambda *call: (
                        transport_calls.append(call) or token_response()
                    ),
                    clock=lambda: 1_800_000_000,
                )

                with self.assertRaisesRegex(
                    ValueError, "family GitHub App metadata"
                ):
                    provider.get()

                self.assertEqual(signer.before, b"family-private-key-marker")
                self.assertEqual(signer.after, signer.before)
                self.assertEqual(
                    signer.snapshot_size, len(b"family-private-key-marker")
                )
                self.assertEqual(signer.seals, required_seals)
                self.assertEqual(signer.write_errno, errno.EPERM)
                self.assertEqual(transport_calls, [])

    def test_close_failure_does_not_orphan_intermediate_descriptors(self) -> None:
        opened: list[int] = []
        real_open = os.open
        real_close = os.close
        injected = False

        def recording_open(*args, **kwargs):  # type: ignore[no-untyped-def]
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def failing_close(descriptor: int) -> None:
            nonlocal injected
            real_close(descriptor)
            if not injected:
                injected = True
                raise OSError("close-secret-marker")

        try:
            with mock.patch(
                "agent_container.family_github_app.os.open",
                side_effect=recording_open,
            ), mock.patch(
                "agent_container.family_github_app.os.close",
                side_effect=failing_close,
            ):
                with self.assertRaises(ValueError) as raised:
                    self.metadata()
            self.assertEqual(
                str(raised.exception), "family GitHub App metadata is invalid"
            )
            self.assertNotIn("close-secret-marker", repr(raised.exception))
            for descriptor in set(opened):
                with self.assertRaises(OSError) as closed:
                    os.fstat(descriptor)
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            for descriptor in set(opened):
                try:
                    real_close(descriptor)
                except OSError:
                    pass

    def test_provider_rejects_non_exact_layout_type_before_side_effects(
        self,
    ) -> None:
        class LayoutSubclass(FamilyStateLayout):
            pass

        layout = LayoutSubclass(self.root, "demo")
        signer = FakeSigner()
        transport_calls: list[object] = []

        with self.assertRaisesRegex(ValueError, "family GitHub App metadata"):
            provider = FamilyInstallationTokenProvider(
                layout,
                signer=signer,
                transport=lambda *call: (
                    transport_calls.append(call) or token_response()
                ),
                clock=lambda: 1_800_000_000,
            )
            provider.get()

        self.assertEqual(signer.calls, [])
        self.assertEqual(transport_calls, [])

    def test_requests_exact_permissions_at_the_fixed_endpoint_and_caches(self) -> None:
        calls: list[tuple[str, dict[str, str], bytes]] = []
        signer = FakeSigner()

        def transport(url, headers, body):  # type: ignore[no-untyped-def]
            calls.append((url, dict(headers), body))
            return token_response()

        provider = FamilyInstallationTokenProvider(
            self.layout,
            signer=signer,
            transport=transport,
            clock=lambda: 1_800_000_000,
        )

        first = provider.get()
        second = provider.get()

        self.assertIs(first, second)
        self.assertEqual(first.expires_at, 1_800_003_600)
        self.assertEqual(first.token, "family-installation-token-marker")
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertEqual(
            url, "https://api.github.com/app/installations/123/access_tokens"
        )
        self.assertEqual(
            headers,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": mock.ANY,
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "agent-container-family-approval",
            },
        )
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(
            json.loads(body),
            {"permissions": {"issues": "write", "metadata": "read"}},
        )
        jwt = headers["Authorization"].removeprefix("Bearer ")
        header, claims, signature = jwt.split(".")
        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode_segment(claims),
            {"exp": 1_800_000_540, "iat": 1_799_999_940, "iss": "Iv1familyabcdefgh"},
        )
        self.assertTrue(signature)
        self.assertIsInstance(signer.calls[0][1], int)

    def test_refreshes_at_the_exact_margin_and_after_invalidation(self) -> None:
        current = [1_800_000_000]
        calls = 0

        def transport(*_):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            expires_at = datetime.fromtimestamp(
                current[0] + 3600, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
            payload = json.loads(token_response().body)
            payload["token"] = f"family-installation-token-{calls:02d}"
            payload["expires_at"] = expires_at
            return HttpResponse(
                201,
                {"Content-Type": "application/json"},
                json.dumps(payload).encode("ascii"),
            )

        signer = FakeSigner()
        provider = FamilyInstallationTokenProvider(
            self.layout,
            signer=signer,
            transport=transport,
            clock=lambda: current[0],
        )
        first = provider.get()
        current[0] = first.expires_at - 301
        self.assertIs(provider.get(), first)
        self.assertEqual(calls, 1)

        current[0] += 1
        second = provider.get()
        self.assertEqual(second.token, "family-installation-token-02")
        self.assertEqual(calls, 2)

        provider.invalidate()
        third = provider.get()
        self.assertEqual(third.token, "family-installation-token-03")
        self.assertEqual(calls, 3)
        self.assertEqual(len(signer.calls), 3)

    def test_token_and_inventory_transports_do_not_follow_real_redirects(self) -> None:
        requests: list[tuple[str, str, str | None]] = []
        with mock.patch(
            "agent_container.github_app.urllib.request.build_opener",
            side_effect=redirecting_opener_factory(requests),
        ):
            token_redirect = development_github_app.github_transport(
                "https://api.github.com/app/installations/123/access_tokens",
                {"Authorization": "Bearer jwt-secret-marker"},
                b"{}",
            )
        self.assertEqual(token_redirect.status, 302)
        self.assertEqual(
            requests,
            [
                (
                    "POST",
                    "https://api.github.com/app/installations/123/access_tokens",
                    "Bearer jwt-secret-marker",
                )
            ],
        )

        requests.clear()
        with mock.patch(
            "agent_container.family_github_app.urllib.request.build_opener",
            side_effect=redirecting_opener_factory(requests),
        ):
            inventory_redirect = family_repository_transport(
                "GET",
                "https://api.github.com/installation/repositories?per_page=100",
                {"Authorization": "Bearer installation-secret-marker"},
                None,
            )
        self.assertEqual(inventory_redirect.status, 302)
        self.assertEqual(
            requests,
            [
                (
                    "GET",
                    "https://api.github.com/installation/repositories?per_page=100",
                    "Bearer installation-secret-marker",
                )
            ],
        )

    def test_rejects_every_missing_extra_or_changed_permission(self) -> None:
        baseline = json.loads(token_response().body)
        cases = (
            {"issues": "write"},
            {"metadata": "read"},
            {"issues": "read", "metadata": "read"},
            {"issues": "write", "metadata": "write"},
            {
                "issues": "write",
                "metadata": "read",
                "contents": "read",
            },
        )
        for permissions in cases:
            with self.subTest(permissions=permissions):
                payload = baseline | {"permissions": permissions}
                response = HttpResponse(
                    201,
                    {"Content-Type": "application/json"},
                    json.dumps(payload).encode("ascii"),
                )
                with self.assertRaisesRegex(ValueError, "token response is invalid"):
                    self.provider(response).get()

    def test_rejects_repository_selection_changes(self) -> None:
        baseline = json.loads(token_response().body)
        for selection in ("all", None, True):
            with self.subTest(selection=selection):
                payload = baseline | {"repository_selection": selection}
                response = HttpResponse(
                    201,
                    {"Content-Type": "application/json"},
                    json.dumps(payload).encode("ascii"),
                )
                with self.assertRaisesRegex(ValueError, "token response is invalid"):
                    self.provider(response).get()

    def test_rejects_redirect_status_content_type_size_and_malformed_token_response(
        self,
    ) -> None:
        marker = "family-secret-response-marker"
        baseline = json.loads(token_response().body)
        cases = (
            HttpResponse(302, {"Location": f"https://evil.invalid/{marker}"}, b""),
            HttpResponse(201, {"Content-Type": "text/plain"}, marker.encode()),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                b"x" * (MAX_RESPONSE_BYTES + 1),
            ),
            HttpResponse(201, {"Content-Type": "application/json"}, b"{"),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                json.dumps(baseline | {"token": "short"}).encode("ascii"),
            ),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                json.dumps(
                    baseline | {"expires_at": "2027-01-15T08:01:00Z"}
                ).encode("ascii"),
            ),
        )
        for response in cases:
            with self.subTest(status=response.status, size=len(response.body)):
                provider = self.provider(response)
                with self.assertRaises((ValueError, RuntimeError)) as raised:
                    provider.get()
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertIsNone(provider._cached)

    def test_token_and_provider_repr_do_not_disclose_secrets(self) -> None:
        provider = self.provider()
        token = provider.get()

        self.assertNotIn(token.token, repr(token))
        self.assertNotIn(token.token, repr(provider))
        self.assertNotIn("family-private-key-marker", repr(provider))


class FamilyRepositoryVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.token = InstallationToken("family-installation-token-marker", 1_800_003_600)
        self.binding = FamilyBinding(Repository("family", "roadmap"), 42)

    def test_verifies_one_exact_repository_using_the_fixed_bounded_inventory(
        self,
    ) -> None:
        calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

        def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
            calls.append((method, url, dict(headers), body))
            return inventory_response()

        verify_family_repository(self.token, self.binding, transport)

        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    "https://api.github.com/installation/repositories?per_page=100",
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": "Bearer family-installation-token-marker",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "agent-container-family-approval",
                    },
                    None,
                )
            ],
        )

    def test_rejects_empty_multiple_or_incomplete_inventory(self) -> None:
        exact = repository_fixture()
        cases = (
            inventory_response([]),
            inventory_response([exact, deepcopy(exact)]),
            inventory_response([exact], total_count=2),
            inventory_response([exact], total_count=101),
            inventory_response(
                [exact],
                headers={
                    "Content-Type": "application/json",
                    "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"',
                },
            ),
        )
        for response in cases:
            with self.subTest(body=response.body, headers=response.headers):
                with self.assertRaises((ValueError, RuntimeError)):
                    verify_family_repository(
                        self.token, self.binding, lambda *_: response
                    )

    def test_rejects_name_only_id_only_rename_and_transfer_matches(self) -> None:
        cases = (
            {"id": 99, "full_name": "family/roadmap"},
            {"id": 42, "full_name": "family/other"},
            {"id": 42, "full_name": "family/renamed-roadmap"},
            {"id": 42, "full_name": "other/roadmap"},
            {"id": True, "full_name": "family/roadmap"},
            {"id": 42, "full_name": "Family/roadmap"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                repository = repository_fixture() | changes
                with self.assertRaisesRegex(ValueError, "repository inventory"):
                    verify_family_repository(
                        self.token,
                        self.binding,
                        lambda *_: inventory_response([repository]),
                    )

    def test_rejects_duplicate_or_ambiguous_inventory_objects(self) -> None:
        duplicate_key = (
            b'{"total_count":1,"repositories":[{"id":42,"id":99,'
            b'"full_name":"family/roadmap"}]}'
        )
        response = HttpResponse(
            200, {"Content-Type": "application/json"}, duplicate_key
        )
        with self.assertRaisesRegex(ValueError, "repository inventory"):
            verify_family_repository(self.token, self.binding, lambda *_: response)

    def test_rejects_redirect_error_content_type_size_and_malformed_inventory(
        self,
    ) -> None:
        marker = "family-inventory-secret-marker"
        cases = (
            HttpResponse(302, {"Location": f"https://evil.invalid/{marker}"}, b""),
            HttpResponse(403, {"Content-Type": "application/json"}, marker.encode()),
            HttpResponse(200, {"Content-Type": "text/plain"}, marker.encode()),
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                b"x" * (MAX_RESPONSE_BYTES + 1),
            ),
            HttpResponse(200, {"Content-Type": "application/json"}, b"{"),
        )
        for response in cases:
            with self.subTest(status=response.status, size=len(response.body)):
                with self.assertRaises((ValueError, RuntimeError)) as raised:
                    verify_family_repository(
                        self.token, self.binding, lambda *_: response
                    )
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertNotIn(self.token.token, str(raised.exception))

    def test_rejects_non_normalized_or_invalid_binding_defensively(self) -> None:
        cases = (
            FamilyBinding(Repository("Family", "roadmap"), 42),
            FamilyBinding(Repository("family", "roadmap"), 0),
            FamilyBinding(Repository("family", "roadmap"), True),
        )
        for binding in cases:
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(ValueError, "family binding is invalid"):
                    verify_family_repository(
                        self.token, binding, lambda *_: inventory_response()
                    )

    def test_transport_allows_only_the_fixed_get_and_bounds_the_body(self) -> None:
        url = "https://api.github.com/installation/repositories?per_page=100"
        for method, candidate, body in (
            ("POST", url, None),
            ("GET", "https://api.github.com/installation/repositories", None),
            ("GET", url + "&page=2", None),
            ("GET", "https://evil.invalid/installation/repositories?per_page=100", None),
            ("GET", url, b"{}"),
        ):
            with self.subTest(method=method, url=candidate, body=body):
                with self.assertRaisesRegex(ValueError, "endpoint is not allowed"):
                    family_repository_transport(method, candidate, {}, body)

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, maximum: int) -> bytes:
                self.maximum = maximum
                return b"x" * maximum

            def close(self) -> None:
                return None

        response = Response()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch(
            "agent_container.family_github_app.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(RuntimeError, "response is too large"):
                family_repository_transport("GET", url, {}, None)
        self.assertEqual(response.maximum, MAX_RESPONSE_BYTES + 1)

    def test_transport_sanitizes_protocol_errors_without_token_disclosure(self) -> None:
        marker = "family-inventory-protocol-secret"
        url = "https://api.github.com/installation/repositories?per_page=100"

        class Opener:
            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                raise http.client.BadStatusLine(marker)

        with mock.patch(
            "agent_container.family_github_app.urllib.request.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaisesRegex(RuntimeError, "repository request failed") as raised:
                family_repository_transport(
                    "GET", url, {"Authorization": f"Bearer {self.token.token}"}, None
                )
        self.assertNotIn(marker, str(raised.exception))
        self.assertNotIn(self.token.token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
