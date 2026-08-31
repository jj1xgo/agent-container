from base64 import urlsafe_b64decode
from copy import deepcopy
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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


def decode_segment(value: str) -> dict[str, object]:
    padding = "=" * (-len(value) % 4)
    return json.loads(urlsafe_b64decode(value + padding))


class FakeSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, Path]] = []

    def sign(self, content: bytes, private_key: Path) -> bytes:
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
            self.metadata(),
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
        with self.assertRaisesRegex(ValueError, "family GitHub App private path"):
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
        with self.assertRaisesRegex(ValueError, "family GitHub App private path"):
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

        with self.assertRaisesRegex(ValueError, "family GitHub App private path"):
            self.metadata()

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

    def test_requests_exact_permissions_at_the_fixed_endpoint_and_caches(self) -> None:
        calls: list[tuple[str, dict[str, str], bytes]] = []
        signer = FakeSigner()

        def transport(url, headers, body):  # type: ignore[no-untyped-def]
            calls.append((url, dict(headers), body))
            return token_response()

        provider = FamilyInstallationTokenProvider(
            self.metadata(),
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
        self.assertEqual(signer.calls[0][1], self.layout.family_private_key_file)

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
