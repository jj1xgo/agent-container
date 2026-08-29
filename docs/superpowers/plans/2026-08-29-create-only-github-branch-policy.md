# Create-only GitHub Branch Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow only first-time creation of unprotected GitHub work branches through the broker and reject every update to an existing remote branch without relying on paid GitHub rulesets.

**Architecture:** Extend the existing receive-pack command gate so an allowed update must target a ref absent from the server advertisement and carry a zero old OID. Treat create-only enforcement as an unconditional broker invariant, remove the ruleset assertion from newly written policy and CLI state, retain exact read compatibility for deployed policies, and update operator evidence without mutating remote state.

**Tech Stack:** Python 3 standard library, Git smart HTTP receive-pack framing, `unittest`/`pytest`, Ruff 0.16.4, Markdown documentation.

**Spec:** `docs/superpowers/specs/2026-08-29-create-only-github-branch-policy-design.md`

## Global Constraints

- Existing remote branches are immutable through the broker, including fast-forward updates.
- New work branches require an absent advertised ref and the object-format-specific zero old OID.
- Protected refs, deletions, non-head refs, stale leases, malformed commands, unsupported capabilities, and existing size/count bounds remain denied.
- The host must not parse untrusted Git objects or trust a container-provided ancestry result.
- No generic GitHub API, repository-administration permission, temporary remote ref, retry, or fallback is added.
- Existing exact policies with `ruleset_confirmed: true` remain readable but the marker has no enforcement effect.
- Newly written policies omit `ruleset_confirmed`; the obsolete CLI flag is removed rather than ignored.
- Audit must not contain object IDs, pack data, capabilities, commit messages, file contents, tokens, or credentials.
- No smoke branch restoration, PR, Issue, cleanup, merge, release, or production mutation occurs during local implementation.

---

### Task 1: Enforce create-only refs in the receive-pack command gate

**Files:**
- Modify: `tests/container/test_git_protocol.py`
- Modify: `src/agent_container/git_protocol.py`
- Test: `tests/container/test_git_protocol.py`

**Interfaces:**
- Consumes: `ReceivePackAdvertisement.refs`, `RefUpdate`, `BrokerPolicy.validate_push_ref()`.
- Produces: unchanged `gate_receive_pack_commands(data, advertisement, policy) -> ReceivePackGate`, with the stronger invariant that every returned update creates an absent branch.

- [ ] **Step 1: Split the existing mixed success test into creation success and existing-ref denial tests**

Replace `test_allows_existing_lease_and_new_work_branch` with a creation-only success test:

```python
def test_allows_only_new_work_branches(self) -> None:
    data = commands(
        (ZERO_OID_SHA1, NEW, "refs/heads/feat/new"),
        (ZERO_OID_SHA1, OTHER, "refs/heads/feat/other"),
    )

    gated = gate_receive_pack_commands(data, self.advertisement, self.policy)

    self.assertEqual(
        tuple(update.ref for update in gated.updates),
        ("refs/heads/feat/new", "refs/heads/feat/other"),
    )
    self.assertTrue(
        all(update.old_oid == ZERO_OID_SHA1 for update in gated.updates)
    )
    self.assertEqual(data[gated.consumed:], b"PACKpayload")
```

Add explicit existing-ref and atomic mixed-request tests:

```python
def test_rejects_every_update_to_an_advertised_work_branch(self) -> None:
    for new_oid in (NEW, OTHER):
        with self.subTest(new_oid=new_oid):
            with self.assertRaisesRegex(ValueError, "already exists"):
                gate_receive_pack_commands(
                    commands((OLD, new_oid, "refs/heads/existing")),
                    self.advertisement,
                    self.policy,
                )

def test_rejects_mixed_create_and_existing_update_as_one_request(self) -> None:
    data = commands(
        (ZERO_OID_SHA1, NEW, "refs/heads/feat/new"),
        (OLD, OTHER, "refs/heads/existing"),
    )

    with self.assertRaisesRegex(ValueError, "already exists"):
        gate_receive_pack_commands(data, self.advertisement, self.policy)
```

Update capability tests that currently use `refs/heads/existing` so their otherwise-valid command creates a unique absent `refs/heads/feat/...` ref with `ZERO_OID_SHA1`; this keeps each test focused on capability behavior.

- [ ] **Step 2: Run the focused tests and verify the new invariant fails**

Run:

```bash
python3 -m pytest tests/container/test_git_protocol.py -q
```

Expected: the advertised-ref denial tests fail because the current gate accepts an exact existing lease; unrelated parser tests remain green.

- [ ] **Step 3: Implement the minimal create-only check**

In `gate_receive_pack_commands`, replace the existing advertised lease acceptance with:

```python
advertised_oid = advertisement.refs.get(ref)
if advertised_oid is not None:
    raise ValueError("Git push ref already exists")
if old_oid != zero_oid:
    raise ValueError("Git push lease does not match")
```

Keep this after ref/type/protection, duplicate, and deletion validation. Do not inspect `new_oid` beyond the existing format and zero-deletion checks.

- [ ] **Step 4: Run protocol and transport regression tests**

Run:

```bash
python3 -m pytest \
  tests/container/test_git_protocol.py \
  tests/container/test_github_broker_transport.py \
  tests/integration/test_github_broker_socket.py -q
```

Expected: PASS. Update transport fixtures only where a nominal successful request incorrectly targets an advertised existing ref. Preserve assertions that denied requests never reach `transport.rpc()` and audit contains only allowlisted fields.

- [ ] **Step 5: Commit the create-only gate**

```bash
git add src/agent_container/git_protocol.py \
  tests/container/test_git_protocol.py \
  tests/container/test_github_broker_transport.py \
  tests/integration/test_github_broker_socket.py
git commit -m "fix: make broker branches create-only"
```

---

### Task 2: Replace ruleset-confirmed state with strict compatibility schemas

**Files:**
- Modify: `tests/container/test_github_broker_runtime.py`
- Modify: `tests/container/test_agentctl.py`
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `src/agent_container/agentctl.py`
- Test: `tests/container/test_github_broker_runtime.py`
- Test: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: `BrokerPolicy`, `_decode_broker_policy()`, `_encode_broker_policy()`, `_add_project()`.
- Produces: new four-key bound policy schema; exact compatibility reads for old four-key global and five-key project-bound schemas carrying `ruleset_confirmed: true`; `agentctl project add --github-broker` without a ruleset flag.

- [ ] **Step 1: Write runtime schema tests before changing serialization**

Rename the legacy tests to state their compatibility purpose, retain fixtures with an exact true marker, and add a new-schema load case:

```python
def test_loads_new_bound_policy_without_ruleset_marker(self) -> None:
    with TemporaryDirectory() as temp:
        path = Path(temp) / "policy.json"
        path.write_text(
            json.dumps(
                {
                    "repository": "jj1xgo/agent-container",
                    "repository_id": 123,
                    "default_branch": "main",
                    "protected_branches": ["main"],
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        record = ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), Path("/handovers")
        )

        policy = load_broker_policy(path, record, "agent-container")

        self.assertEqual(policy.repository_id, 123)
```

Change the serialization expectation to:

```python
{
    "repository": "jj1xgo/agent-container",
    "repository_id": 123,
    "default_branch": "main",
    "protected_branches": ["main"],
}
```

Add table-driven rejection for `ruleset_confirmed: false`, a marker combined with any unknown key, and a new schema containing any unknown key. Keep duplicate-key and unsafe-file tests unchanged.

- [ ] **Step 2: Write CLI tests for removal of the obsolete assertion**

Update successful broker registration invocations to omit
`--confirm-force-push-ruleset`. Add a parser test equivalent to:

```python
with self.assertRaises(SystemExit):
    parser().parse_args(
        [
            "project", "add", "jj1xgo/agent-container",
            "--handover-root", "/handovers",
            "--github-broker",
            "--github-repository-id", "123",
            "--confirm-force-push-ruleset",
        ]
    )
```

Remove tests expecting “GitHub force-push ruleset confirmation is required.” Preserve the requirement that new broker projects supply a positive repository ID and that broker-only options fail without `--github-broker`.

- [ ] **Step 3: Run focused tests and verify they fail for the old schema/CLI**

Run:

```bash
python3 -m pytest \
  tests/container/test_github_broker_runtime.py \
  tests/container/test_agentctl.py -q
```

Expected: failures show the writer still emits `ruleset_confirmed`, the decoder does not accept the new schema, registration still requires the flag, and argparse still accepts it.

- [ ] **Step 4: Implement exact old/new schema decoding and new-only encoding**

In `_decode_broker_policy`, define exact key sets:

```python
base_keys = {"repository", "default_branch", "protected_branches"}
new_bound_keys = base_keys | {"repository_id"}
legacy_global_keys = base_keys | {"ruleset_confirmed"}
legacy_bound_keys = new_bound_keys | {"ruleset_confirmed"}
```

Accept only those three schemas. For either legacy schema, require
`payload["ruleset_confirmed"] is True`; never copy that value into
`BrokerPolicy`. Require `repository_id` exactly when its key is present. Make
`_encode_broker_policy` write only `new_bound_keys`.

- [ ] **Step 5: Remove the CLI flag and ruleset parameter flow**

Delete the parser option, the `ruleset_confirmed` parameter from `_add_project`,
its precondition, the `arguments.confirm_force_push_ruleset` broker-option
check, and the argument passed from `main`. Do not add an ignored compatibility
flag. Adjust interrupted-registration equality tests so a legacy on-disk policy
can still be upgraded to the new bound schema without changing sibling files.

- [ ] **Step 6: Run policy, CLI, doctor, and registration recovery tests**

Run:

```bash
python3 -m pytest \
  tests/container/test_github_broker_runtime.py \
  tests/container/test_agentctl.py \
  tests/integration/test_agentctl_cli.py -q
```

Expected: PASS, including legacy production-policy loading, project-bound smoke-policy loading, strict invalid schema rejection, no marker in new writes, and no sibling-file mutation.

- [ ] **Step 7: Commit the policy and CLI migration**

```bash
git add src/agent_container/agentctl.py \
  src/agent_container/github_broker_runtime.py \
  tests/container/test_agentctl.py \
  tests/container/test_github_broker_runtime.py \
  tests/integration/test_agentctl_cli.py
git commit -m "fix: remove broker ruleset dependency"
```

---

### Task 3: Align operator documentation and preserve failed-gate evidence

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/phase3-github-broker.md`
- Modify: `docs/phase3-github-broker-smoke-test.md`
- Modify: `docs/phase4-stabilization-smoke-test.md`
- Modify: `docs/superpowers/specs/2026-08-25-phase-3-github-broker-design.md`
- Modify: `docs/superpowers/specs/2026-08-29-project-scoped-github-repository-binding-design.md`
- Modify: `docs/superpowers/specs/2026-08-29-phase4-stabilization-release-design.md`
- Modify: `docs/superpowers/plans/2026-08-25-phase-3-github-broker.md`
- Modify: `docs/superpowers/plans/2026-08-29-project-scoped-github-repository-binding.md`
- Modify: `docs/superpowers/plans/2026-08-29-phase4-stabilization-release.md`
- Modify: `tests/container/test_docs.py`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: the approved create-only design and the observed Phase 4 HTTP 403/force-push evidence.
- Produces: executable registration/smoke instructions with no ruleset flag, accurate workflow limitations, and retained credential-free failure evidence.

- [ ] **Step 1: Write documentation assertions first**

Update `tests/container/test_docs.py` so current operator commands must not
contain `--confirm-force-push-ruleset`, current policy examples must not require
`ruleset_confirmed`, and normative docs contain the create-only constraint.
Use assertions over the relevant current documents, for example:

```python
for path in current_operator_docs:
    body = path.read_text(encoding="utf-8")
    self.assertNotIn("--confirm-force-push-ruleset", body)
self.assertIn("既存branchへのupdateを拒否", phase3_body)
self.assertIn("HTTP 403", phase4_body)
self.assertIn("force push", phase4_body)
```

Historical failure evidence may mention the unavailable ruleset, but must not
present it as a currently required or successful gate.

- [ ] **Step 2: Run docs tests and verify they fail**

Run:

```bash
python3 -m pytest tests/container/test_docs.py -q
```

Expected: failures identify the old registration flag, policy marker examples,
and ruleset-dependent gate instructions.

- [ ] **Step 3: Update normative security and registration documentation**

Replace ruleset prerequisites with these exact operational facts throughout
README, Phase 3, linked specs, and active plans:

- only absent unprotected `refs/heads/*` refs with zero old OID may be created;
- every update to an advertised branch is denied, including fast-forward;
- subsequent work requires a new branch and optionally a new PR;
- new policy files have no ruleset marker;
- old exact true-marker schemas are compatibility input only;
- no paid GitHub branch setting is claimed by doctor.

Remove the obsolete flag from every executable command. Do not rewrite dated
observations that explain why the architecture changed.

- [ ] **Step 4: Record the Phase 4 failed negative gate accurately**

In `docs/phase4-stabilization-smoke-test.md`, retain the successful initial
branch push, protected/delete/tag denials, receive-pack hang and fix, and record:

- private ruleset inventory: FAIL/PARTIAL with HTTP 403 plan limitation;
- unrelated-history update: FAIL because GitHub accepted it;
- disposable remote branch changed;
- final OID check inside the stopped runtime was not run, followed by the
  separate bounded host observation that the remote branch changed;
- no retry, restoration, PR, Issue, cleanup, or release followed.

Do not include repository numeric IDs, credentials, capabilities, image IDs,
pack data, or commit contents.

- [ ] **Step 5: Run docs tests and scan all active instructions**

Run:

```bash
python3 -m pytest tests/container/test_docs.py -q
grep -RIn -- '--confirm-force-push-ruleset' README.md CHANGELOG.md docs src tests
```

Expected: docs tests PASS. The grep may find the approved create-only design or
historical explanation saying the option was removed, but no executable command
or current assertion may require it.

- [ ] **Step 6: Commit documentation and evidence**

```bash
git add README.md CHANGELOG.md docs tests/container/test_docs.py
git commit -m "docs: document create-only broker pushes"
```

---

### Task 4: Verify the complete local change and prepare the host gate

**Files:**
- Modify only if verification exposes a defect in an already scoped file.
- Verify: all files changed by Tasks 1–3.

**Interfaces:**
- Consumes: committed create-only implementation, compatibility migration, and documentation.
- Produces: fresh local verification evidence and a credential-free host test procedure awaiting separate mutation approval.

- [ ] **Step 1: Run Ruff 0.16.4**

Use the already installed reviewed Ruff executable. Run:

```bash
ruff --version
ruff check src tests
```

Expected: the first command prints `ruff 0.16.4` and the second command passes.
If that exact executable is absent, stop; do not download or substitute a
different version during verification.

- [ ] **Step 2: Run focused security regressions**

```bash
python3 -m pytest \
  tests/container/test_git_protocol.py \
  tests/container/test_github_broker_transport.py \
  tests/container/test_github_broker_runtime.py \
  tests/container/test_agentctl.py \
  tests/container/test_docs.py \
  tests/integration/test_github_broker_socket.py \
  tests/integration/test_agentctl_cli.py -q
```

Expected: PASS with only already documented environment-dependent skips.

- [ ] **Step 3: Run the complete suite and repository checks**

```bash
python3 -m pytest -q
git diff --check
git status --short --branch
```

Expected: complete suite PASS, no whitespace errors, and no uncommitted files.

- [ ] **Step 4: Review the security boundary before any host action**

Review the final diff specifically for:

- no path permits an advertised branch update to reach `transport.rpc()`;
- no legacy policy marker can disable create-only enforcement;
- no ignored compatibility CLI flag remains;
- no audit/log/error includes OIDs or request bodies;
- docs do not imply that GitHub rulesets protect private repositories;
- no production repository or external-state command appears in local tasks.

Expected: no Critical, Important, or Minor findings. Fix any finding with a new
failing test and a separate scoped commit before continuing.

- [ ] **Step 5: Draft, but do not execute, the corrected host gate**

The host handoff must stop for fresh approval after a reviewed image is built.
It names one new disposable branch and separates approvals for:

1. first creation push;
2. a subsequent ordinary fast-forward update denial;
3. an unrelated-history update denial;
4. bounded read-only remote OID verification;
5. optional restoration/deletion cleanup.

The script must stop immediately if either existing-branch update succeeds.
It must not retry, fall back, open a PR, read Issues, restore/delete refs, merge,
or release without the corresponding later approval.

If verification exposes a defect, return to the task that owns the affected
interface, add a failing regression test there, apply one scoped correction,
rerun that task and Task 4, and commit with that task's exact file list. Do not
create an empty verification commit.
