# Project-scoped GitHub Repository Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each GitHub broker project to its own positive repository ID while sharing one host-private App identity, preserving existing single-repository projects and safely resuming the interrupted Phase 4 smoke registration.

**Architecture:** Extend the host-only project broker policy with an optional validated repository ID. New projects persist an explicit ID; legacy policies fall back to the global App metadata ID. At runtime, combine the global client/installation/private key with the project policy ID before requesting the existing exact-one-repository token. Allow one narrowly defined atomic upgrade only for clone-before-metadata interrupted state.

**Tech Stack:** Python 3.11+ standard library, `argparse`, immutable dataclasses, JSON files with owner/mode/symlink validation, `unittest`, rootless Podman, GitHub App installation tokens.

**Spec:** `docs/superpowers/specs/2026-08-29-project-scoped-github-repository-binding-design.md`

## Global Constraints

- Keep global `app.json` and `private-key.pem` as the only App identity/key state; never duplicate the private key.
- Every installation token remains limited to one repository and the exact permissions `checks:read`, `contents:write`, `issues:read`, `metadata:read`, and `pull_requests:write`.
- Do not add repository IDs, credentials, tokens, JWTs, private keys, capabilities, response bodies, or Git payloads to audit or container mounts.
- Preserve legacy broker policy runtime behavior through global repository-ID fallback.
- New broker projects require an explicit positive repository ID.
- Refuse unknown policy fields, duplicate JSON keys, booleans, zero/negative IDs, symlinks, wrong owners, and modes other than `0600`.
- Do not modify, delete, or retry the host partial state until Tasks 1–5 pass review and Task 6 receives fresh approval.
- A failed external operation is never automatically retried.
- Keep all existing Git/PR/Checks/Issue operation and branch-policy boundaries unchanged.

---

### Task 1: Extend the in-memory and persisted broker policy

**Files:**
- Modify: `src/agent_container/github_broker_policy.py`
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `tests/container/test_github_broker_policy.py`
- Modify: `tests/container/test_github_broker_runtime.py`

**Interfaces:**
- Consumes: existing `BrokerPolicy.create(...)`, `load_broker_policy(...)`, and `write_broker_policy(...)`.
- Produces: `validate_repository_id(value: int) -> int`; `BrokerPolicy.repository_id: int | None`; strict legacy/new policy loading; new-policy serialization with an explicit ID.

- [ ] **Step 1: Add failing repository-ID validation and policy-model tests**

Add focused tests equivalent to:

```python
def test_policy_accepts_positive_repository_id(self) -> None:
    policy = BrokerPolicy.create(
        project_id="smoke",
        repository="jj1xgo/agent-container-smoke",
        repository_id=123,
        default_branch="main",
        protected_branches=("main",),
    )
    self.assertEqual(policy.repository_id, 123)

def test_policy_rejects_invalid_repository_ids(self) -> None:
    for value in (True, False, 0, -1, "123", None):
        with self.subTest(value=value), self.assertRaises(ValueError):
            BrokerPolicy.create(
                project_id="smoke",
                repository="jj1xgo/agent-container-smoke",
                repository_id=value,
                default_branch="main",
                protected_branches=("main",),
                require_repository_id=True,
            )
```

Use a separate legacy test to prove `repository_id=None` is accepted only when
`require_repository_id=False`.

- [ ] **Step 2: Run the focused model test and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_policy -v
```

Expected: errors because the new argument/property/validator do not exist.

- [ ] **Step 3: Implement the minimal immutable policy model**

Add:

```python
def validate_repository_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("GitHub repository ID is invalid")
    return value
```

Extend `BrokerPolicy` with `repository_id: int | None`, and extend `create`
with keyword-only `repository_id: int | None = None` and
`require_repository_id: bool = False`. Validate a present ID unconditionally;
raise the same generic error if it is absent while required.

- [ ] **Step 4: Add failing strict loader/writer tests**

In `test_github_broker_runtime.py`, cover both exact schemas:

```python
legacy = {
    "repository": "jj1xgo/agent-container",
    "default_branch": "main",
    "protected_branches": ["main"],
    "ruleset_confirmed": True,
}
bound = legacy | {"repository_id": 123}
```

Assert legacy loads with `repository_id is None`, bound loads with `123`, and
new writes include exactly the five bound keys. Add subtests rejecting
`repository_id` values `True`, `0`, `-1`, and `"123"`, plus one unknown-key
case.

- [ ] **Step 5: Run the loader tests and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_runtime.BrokerRuntimePolicyTest -v
```

Expected: bound schema is rejected or the ID is not preserved.

- [ ] **Step 6: Implement dual-schema loading and bound serialization**

Make `load_broker_policy` accept only the exact legacy key set or the exact
bound key set. Pass the optional ID into `BrokerPolicy.create`. Make
`write_broker_policy` refuse `policy.repository_id is None` and serialize the
five-key bound schema. Preserve `O_EXCL`, `O_NOFOLLOW`, mode `0600`, `fsync`,
and ASCII JSON behavior.

- [ ] **Step 7: Run focused tests and confirm GREEN**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_policy \
  tests.container.test_github_broker_runtime.BrokerRuntimePolicyTest -v
git diff --check
```

- [ ] **Step 8: Commit Task 1**

```bash
git add \
  src/agent_container/github_broker_policy.py \
  src/agent_container/github_broker_runtime.py \
  tests/container/test_github_broker_policy.py \
  tests/container/test_github_broker_runtime.py
git commit -m "feat: bind broker policy to repository ID"
```

### Task 2: Compose project repository identity into runtime tokens

**Files:**
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `tests/container/test_github_broker_runtime.py`
- Test: `tests/container/test_github_app.py`

**Interfaces:**
- Consumes: `BrokerPolicy.repository_id` from Task 1 and immutable `GitHubAppMetadata`.
- Produces: `broker_token_metadata(app: GitHubAppMetadata, policy: BrokerPolicy) -> GitHubAppMetadata`; runtime transports whose token provider targets the project-bound ID or legacy fallback.

- [ ] **Step 1: Write failing runtime-composition tests**

Add tests using real immutable values, not opaque `object()` policies:

```python
app = GitHubAppMetadata(
    client_id="Iv1abcdefghijk",
    installation_id=11,
    repository_id=22,
    private_key=Path("/private-key.pem"),
)
bound = BrokerPolicy.create(
    project_id="smoke",
    repository="jj1xgo/agent-container-smoke",
    repository_id=33,
    default_branch="main",
    protected_branches=("main",),
)
self.assertEqual(broker_token_metadata(app, bound).repository_id, 33)
self.assertEqual(app.repository_id, 22)
```

Add a legacy policy assertion that returns repository ID `22`. Add a two-project
test proving IDs `33` and `44` produce separate metadata while retaining the
same client ID, installation ID, and private-key path.

- [ ] **Step 2: Run the runtime test and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_runtime.BrokerRuntimeConstructionTest -v
```

Expected: `broker_token_metadata` is missing and `UploadPackBrokerRuntime.create`
still passes the global metadata unchanged.

- [ ] **Step 3: Implement runtime composition**

Use `dataclasses.replace` without reading or copying the key body:

```python
def broker_token_metadata(
    app: GitHubAppMetadata, policy: BrokerPolicy
) -> GitHubAppMetadata:
    repository_id = (
        app.repository_id
        if policy.repository_id is None
        else policy.repository_id
    )
    return replace(app, repository_id=repository_id)
```

In `UploadPackBrokerRuntime.create`, load the App and policy, derive token
metadata, and pass only the derived metadata to `InstallationTokenProvider`.
Keep `BrokerSession`, all transports, and audit unchanged.

- [ ] **Step 4: Run runtime and token-provider regression tests**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_runtime \
  tests.container.test_github_app -v
git diff --check
```

Expected: all tests pass; exact-one-repository and exact-permission token tests
remain green.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/agent_container/github_broker_runtime.py \
  tests/container/test_github_broker_runtime.py
git commit -m "feat: scope App tokens by broker project"
```

### Task 3: Require an explicit repository ID for new broker projects

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: bound policy creation/writing from Task 1.
- Produces: CLI option `--github-repository-id`; `_positive_repository_id(value: str) -> int`; `_add_project(..., github_repository_id: int | None = None)`.

- [ ] **Step 1: Add failing CLI-boundary tests**

Add tests for:

```python
self.assertEqual(
    parser().parse_args([
        "project", "add", "jj1xgo/agent-container-smoke",
        "--handover-root", "/handovers",
        "--github-broker", "--github-repository-id", "123",
    ]).github_repository_id,
    123,
)
```

Through `main`, assert each of `0`, `-1`, `true`, `1.0`, and whitespace fails
before runner calls/state creation. Assert the option without `--github-broker`
fails. Assert a new broker project without the option fails before Podman or
state mutation.

- [ ] **Step 2: Run the CLI tests and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlProjectTest \
  tests.container.test_agentctl.AgentCtlParserTest -v
```

Expected: parser rejects the new option or new-project registration still
accepts a missing ID.

- [ ] **Step 3: Implement parsing and new-project validation**

Add:

```python
def _positive_repository_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or value.startswith("0"):
        raise argparse.ArgumentTypeError("GitHub repository ID must be a positive integer")
    parsed = int(value)
    validate_repository_id(parsed)
    return parsed
```

Add `--github-repository-id` with this type. Treat it as a broker-only option.
Pass it to `_add_project`. When no workspace/project metadata/policy exists,
require it before `_podman_preflight` or directory creation. Create new
`BrokerPolicy` values with `require_repository_id=True`, so all new writes use
the bound schema.

For a completed exact legacy project, preserve idempotent `project add`
behavior without requiring the new option; do not rewrite its policy.

- [ ] **Step 4: Update existing broker-registration tests explicitly**

Add `--github-repository-id 456` to tests that create a new broker project and
assert the written policy contains `repository_id: 456`. Keep at least one
completed legacy-project test without the option to prove compatibility.

- [ ] **Step 5: Run agentctl tests and confirm GREEN**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v
git diff --check
```

- [ ] **Step 6: Commit Task 3**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: require broker repository binding"
```

### Task 4: Atomically upgrade only interrupted legacy registration

**Files:**
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_github_broker_runtime.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: an exact legacy `BrokerPolicy`, explicit repository ID, and absence of `project.json`/workspace.
- Produces: `upgrade_legacy_broker_policy(path: Path, existing: BrokerPolicy, requested: BrokerPolicy) -> None`, which changes only a verified legacy policy via atomic replacement.

- [ ] **Step 1: Write failing atomic-upgrade unit tests**

Create a mode `0600` legacy file and a sibling `smoke-fixtures.json`. Call the
new function and assert:

```python
upgraded = load_broker_policy(path, record, "agent-container-smoke")
self.assertEqual(upgraded.repository_id, 123)
self.assertEqual(sibling.read_bytes(), original_sibling)
self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])
```

Add refusal tests for mismatched repository/default/protected branches,
already-bound same/different ID, symlink target, and wrong mode. Snapshot the
original bytes and assert they are unchanged after every failure.

- [ ] **Step 2: Run upgrade unit tests and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_runtime.BrokerPolicyUpgradeTest -v
```

Expected: the upgrade function does not exist.

- [ ] **Step 3: Implement secure atomic replacement**

Serialize the requested bound policy using the same encoder as new writes.
Create a random same-directory temporary file with `O_WRONLY | O_CREAT |
O_EXCL` and `O_NOFOLLOW` when available, mode `0600`; write all bytes, `fsync`
the file, close it, revalidate the original path, then `os.replace` the temp
path over the original and `fsync` an `O_DIRECTORY` descriptor for the parent.
On every pre-replace failure, close/unlink only the temporary file. Never touch
sibling files.

- [ ] **Step 4: Write failing end-to-end interrupted-state tests**

Prepare the exact observed shape: private project directory, credential-free
sibling manifest, legacy matching policy, existing handover directory, absent
`project.json`, and absent workspace. Pass `--github-repository-id 123`; make
the fake clone runner create a valid workspace. Assert policy upgrade occurs,
manifest is unchanged, clone runs once, and project metadata is eventually
written.

Add cases where `project.json` exists, workspace exists/is a symlink, policy
mismatches, or explicit ID differs. Assert runner is never called and policy
bytes remain unchanged.

- [ ] **Step 5: Run interrupted-state tests and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -k interrupted -v
```

Expected: existing legacy policy mismatches the requested bound policy or is
not upgraded.

- [ ] **Step 6: Integrate the narrow resume gate**

In `_add_project`, before upgrading, require both `project_file` and workspace
to be absent and non-symlinks. Require all non-ID policy fields to equal the
requested bound policy. Call `upgrade_legacy_broker_policy` only for this
shape. Existing bound-equal policy may continue; bound-different and completed
legacy policies are never rewritten.

- [ ] **Step 7: Run focused and full agentctl/runtime tests**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_runtime \
  tests.container.test_agentctl -v
git diff --check
```

- [ ] **Step 8: Commit Task 4**

```bash
git add \
  src/agent_container/github_broker_runtime.py \
  src/agent_container/agentctl.py \
  tests/container/test_github_broker_runtime.py \
  tests/container/test_agentctl.py
git commit -m "fix: resume bound broker registration safely"
```

### Task 5: Expose local binding status and update operator contracts

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`
- Modify: `tests/container/test_docs.py`
- Modify: `README.md`
- Modify: `docs/phase3-github-broker.md`
- Modify: `docs/phase4-stabilization-smoke-test.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: explicit/legacy policy state from Tasks 1–4.
- Produces: doctor detail `project repository binding valid` or `legacy global repository binding valid`; documented CLI/migration/security boundary; executable documentation contract.

- [ ] **Step 1: Add failing doctor and documentation tests**

Update the broker doctor test to expect:

```text
PASS  github-broker: local App and project repository binding valid
```

Add a legacy-policy doctor test expecting:

```text
PASS  github-broker: local App and legacy global repository binding valid
```

Extend `Phase4DocumentationTest` to require all of these phrases in the
operator/smoke documentation: `--github-repository-id`, `project-scoped`,
`legacy global fallback`, `upload-discovery`, and `remote App selection`.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlRunDoctorTest.test_doctor_github_broker_validates_local_state_without_gh_credentials \
  tests.container.test_docs.Phase4DocumentationTest -v
```

- [ ] **Step 3: Implement doctor detail without remote overclaiming**

Use the loaded policy's `repository_id is None` state to select the exact
bounded or legacy PASS detail. Do not print the numeric ID or add a network
probe. Keep the existing warning that doctor does not prove remote installation,
permission, repository selection, or ruleset state.

- [ ] **Step 4: Update documentation and changelog**

Document the new project-add option with a bounded `gh repo view ... --json
databaseId --jq .databaseId` host inventory step. State that the ID is passed
explicitly but never written to audit/container output. Record the diagnosed
Phase 4 `upload-discovery` failure, project-scoped fix, legacy fallback, exact
partial-state recovery gate, and the fact that host retry still requires fresh
approval. Do not mark the host gate PASS before it is rerun.

- [ ] **Step 5: Run documentation and broker regressions**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_docs \
  tests.container.test_agentctl \
  tests.container.test_github_app \
  tests.container.test_github_broker_policy \
  tests.container.test_github_broker_runtime \
  tests.container.test_github_git_transport \
  tests.container.test_github_broker_transport \
  tests.integration.test_github_broker_socket -v
git diff --check
```

- [ ] **Step 6: Commit Task 5**

```bash
git add \
  CHANGELOG.md README.md \
  docs/phase3-github-broker.md \
  docs/phase4-stabilization-smoke-test.md \
  src/agent_container/agentctl.py \
  tests/container/test_agentctl.py \
  tests/container/test_docs.py
git commit -m "docs: explain project-scoped broker binding"
```

- [ ] **Step 7: Run complete verification and request code review**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
git status --short --branch
```

Record exact pass/skip counts. Review the complete range after `5e5f8df` for
spec compliance, security boundary, backward compatibility, and absence of
repository IDs in audit/container mounts. Address review findings through at
most five focused fix rounds, rerunning affected tests after every round.

### Task 6: Recover the host registration and return to Phase 4 Task 4

**Files:**
- Read host-only: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`
- Modify host-only: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json`
- Create host-only: `$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke/`
- Create host-only after success: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json`

**Interfaces:**
- Consumes: reviewed release-candidate image/code, observed partial state, exact smoke repository ID, and fresh recovery approval.
- Produces: completed `agent-container-smoke` project registration using an explicit project-scoped repository binding.

- [ ] **Step 1: Rebuild and verify the reviewed candidate on the host**

From the host checkout containing the reviewed commits:

```bash
set -eu
bin/agentctl build >/dev/null
podman run --rm localhost/agent-container:dev python3 -m agent_container.agentctl --version >/dev/null
podman run --rm localhost/agent-container:dev agent-github --help >/dev/null
printf '%s\n' 'reviewed_candidate_valid=true'
```

Stop on any failure. Do not retry automatically.

- [ ] **Step 2: Inventory the exact partial state read-only**

Require: project directory `0700`; policy and fixture manifest regular,
non-symlink, current-user-owned, `0600`; absent `project.json`; absent
workspace; existing handover directory `0700`. Load the policy through the
reviewed code and confirm exact repository/default/protected/ruleset fields and
legacy `repository_id is None`. Validate the manifest exact keys and fixture
numbers without printing its body.

```bash
set -eu
test -d "$AGENT_CONTAINER_HOME/projects/agent-container-smoke"
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke"
test "$(stat -c '%a:%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke")" = "700:$(id -u)"
test -f "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json"
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json"
test "$(stat -c '%a:%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/github-broker.json")" = "600:$(id -u)"
test -f "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json"
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json"
test "$(stat -c '%a:%u' "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json")" = "600:$(id -u)"
test -d "$AGENT_HANDOVER_ROOT/agent-container-smoke"
test ! -L "$AGENT_HANDOVER_ROOT/agent-container-smoke"
test "$(stat -c '%a:%u' "$AGENT_HANDOVER_ROOT/agent-container-smoke")" = "700:$(id -u)"
test ! -e "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json"
test ! -L "$AGENT_CONTAINER_HOME/projects/agent-container-smoke/project.json"
test ! -e "$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke"
test ! -L "$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke"
printf '%s\n' 'partial_state_filesystem_valid=true'
```

Use the reviewed loader for the policy and an exact duplicate-rejecting JSON
check for the fixture. Print only boolean markers:

```bash
AGENT_CONTAINER_HOME="$AGENT_CONTAINER_HOME" PYTHONPATH=src python3 - <<'PY'
import json
import os
from pathlib import Path

from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_runtime import load_broker_policy
from agent_container.state import ProjectRecord, Repository


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate fixture key")
        value[key] = item
    return value


root = Path(os.environ["AGENT_CONTAINER_HOME"])
project_dir = root / "projects/agent-container-smoke"
repository = Repository.parse("jj1xgo/agent-container-smoke")
record = ProjectRecord(repository, Path("/unused"))
policy = load_broker_policy(
    project_dir / "github-broker.json", record, "agent-container-smoke"
)
expected_policy = BrokerPolicy.create(
    project_id="agent-container-smoke",
    repository="jj1xgo/agent-container-smoke",
    default_branch="main",
    protected_branches=("main",),
)
if policy.repository_id is not None or policy != expected_policy:
    raise ValueError("legacy policy mismatch")
print("legacy_policy_valid=true")

payload = json.loads(
    (project_dir / "smoke-fixtures.json").read_bytes(),
    object_pairs_hook=unique_object,
)
expected_static = {
    "repository": "jj1xgo/agent-container-smoke",
    "default_branch": "main",
    "open_body_sentinel": "phase4-open-body-sentinel",
    "closed_body_sentinel": "phase4-closed-body-sentinel",
    "excluded_field_sentinel": "phase4-excluded-field-sentinel",
    "pull_request_sentinel": "phase4-pr-exclusion-sentinel",
}
expected_keys = set(expected_static) | {
    "open_issue",
    "closed_issue",
    "pull_request",
}
if not isinstance(payload, dict) or set(payload) != expected_keys:
    raise ValueError("fixture schema mismatch")
if any(payload[key] != value for key, value in expected_static.items()):
    raise ValueError("fixture identity mismatch")
expected_numbers = {
    "open_issue": 1,
    "closed_issue": 2,
    "pull_request": 3,
}
if any(payload[key] != value for key, value in expected_numbers.items()):
    raise ValueError("fixture number mismatch")
print("fixture_manifest_valid=true")
PY
```

- [ ] **Step 3: Resolve the repository ID without recording it**

```bash
set +x
set -eu
smoke_repository_id=$(
  GH_CONFIG_DIR="$AGENT_CONTAINER_HOME/gh" \
    gh repo view jj1xgo/agent-container-smoke \
      --json databaseId --jq .databaseId
)
case "$smoke_repository_id" in
  ''|*[!0-9]*) exit 1 ;;
esac
test "$smoke_repository_id" -gt 0 || exit 1
printf '%s\n' 'smoke_repository_id_valid=true'
# STOP: fresh approval required before registration.
```

Do not add the value to docs, audit, handover, or command transcripts. Remain
stopped after the boolean marker; repository lookup approval does not authorize
registration.

- [ ] **Step 4: Obtain fresh recovery approval**

Name the exact repository, project ID, legacy policy atomic upgrade, broker
clone, project metadata creation, and existing sibling manifest preservation.
State that no fixture, production repository, App setting, ruleset, or release
mutation is included.

The shared installation must retain production and smoke selected repositories.
Do not deselect the production repository. each installation token narrows to
exactly one project repository ID.

- [ ] **Step 5: Resume registration once**

```bash
if ! bin/agentctl project add jj1xgo/agent-container-smoke \
  --project agent-container-smoke \
  --handover-root "$AGENT_HANDOVER_ROOT" \
  --github-broker \
  --github-repository-id "$smoke_repository_id" \
  --default-branch main \
  --protected-branch main \
  --confirm-force-push-ruleset; then
  unset smoke_repository_id
  exit 1
fi
unset smoke_repository_id
# shell tracing may resume only after the ID is unset
```

On failure, stop and inspect only fixed stderr plus allowlisted audit
`{timestamp,project,repository,operation,status,stage}`. Do not retry.

- [ ] **Step 6: Verify registration and smoke/production doctors**

```bash
bin/agentctl doctor agent-container-smoke --github-broker
bin/agentctl doctor agent-container-smoke --agent claude --github-broker
bin/agentctl doctor agent-container --github-broker
```

Require exact HTTPS origin, bound-policy doctor detail, required checks PASS,
and only the documented network-policy warning. Confirm the fixture manifest
is unchanged and the production project's doctor remains green.

- [ ] **Step 7: Record bounded evidence and resume the original Phase 4 plan**

Update the Phase 4 evidence row only from observed results. Preserve the first
`upload-discovery` failure and diagnosis. Then return to Task 4 Step 5 of
`docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md`; do not
start Git/PR mutations without their separate approvals.
