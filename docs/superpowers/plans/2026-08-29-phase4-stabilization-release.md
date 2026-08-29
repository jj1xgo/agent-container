# Phase 4 Stabilization and v0.4.0 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile the documented product scope with the shipped security boundary, close the remaining GitHub broker host gates in a dedicated private repository, and publish the verified result as `v0.4.0`.

**Architecture:** Keep production interfaces unchanged. First make the scope and host-gate contract executable through documentation tests, then prepare a host-managed private fixture repository and register it as an independent broker project. Run Git, PR, Issue, cleanup, and denial gates only inside that repository, record observations without credentials, merge the release documentation, and create the tag and GitHub Release only after a separate final approval.

**Tech Stack:** Python 3.11+ `unittest`, Markdown contract tests, rootless Podman, Git 2.53+, `agentctl`, project-scoped GitHub App broker, `agent-github`, GitHub CLI for explicitly approved host administration, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-29-phase4-stabilization-release-design.md`

**2026-08-29 security correction and stop state:** Broker push is create-only:
only an absent unprotected branch with a zero old OID may be created. Brokerは
既存branchへのupdateを拒否し、fast-forwardもnon-fast-forwardも拒否する。
Further work uses a 新しいbranch and, when needed, a new PR. The private
ruleset inventory returned HTTP 403 upgrade-or-public, and the original
unrelated-history negative push was accepted. The remote disposable branch
changed; no retry, restoration, PR, Issue, cleanup, or release followed. Do not
resume the external plan until the corrected implementation is reviewed and a
new disposable branch or an explicitly approved restoration is authorized.

## Global Constraints

- Do not add a family Issue write interface, egress proxy, domain allowlist, new platform, merge endpoint, release endpoint, repository administration, or generic GitHub API proxy.
- The dedicated repository is `jj1xgo/agent-container-smoke`, private, reusable, and never deleted automatically.
- Every GitHub mutation requires a fresh approval that names the repository, operation, and affected ref or object.
- Host fixture administration may use `gh`; broker smoke must not fall back to `gh`, environment tokens, SSH agent, or host credential helpers.
- Never print or record credential, token, private key, JWT, capability value, length, prefix, suffix, or hash.
- Record only bounded operation output and allowlisted audit fields. Do not capture GitHub error bodies, raw headers, packfiles, Issue content in audit, or environment listings.
- A failed external operation is not retried automatically. Stop at the failed gate and diagnose from fixed error and allowlisted audit metadata.
- Do not mark an unobserved check `PASS`. Use `PARTIAL` or `not run` with the reason.
- Do not move, overwrite, or reuse a published tag. A post-release correction uses a new version.
- Keep the existing `WARN network-policy` until a separately approved egress design is implemented.

---

### Task 1: Make the Phase 4 documentation contract fail

**Files:**
- Modify: `tests/container/test_docs.py`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: `Phase3DocumentationTest` and repository-root `ROOT` path.
- Produces: `Phase4DocumentationTest`, which requires the approved spec, scope reconciliation, dedicated smoke guide, release gate, and explicit future deferrals.

- [ ] **Step 1: Add the failing documentation contract**

Append this class before the next non-documentation test class in `tests/container/test_docs.py`:

```python
class Phase4DocumentationTest(unittest.TestCase):
    def test_phase4_scope_and_release_contract(self) -> None:
        initial = (
            ROOT / "docs/superpowers/specs/2026-08-22-agent-container-design.md"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        operator = (ROOT / "docs/phase3-github-broker.md").read_text(
            encoding="utf-8"
        )
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for body in (initial, readme, operator):
            self.assertIn("family Issue create/comment", body)
            self.assertIn("将来Phase", body)
            self.assertIn("domain allowlist", body)
        for required in (
            "jj1xgo/agent-container-smoke",
            "private repository",
            "stale client",
            "Pull Request除外",
            "non-fast-forward",
            "v0.4.0",
            "最終承認",
        ):
            self.assertIn(required, smoke)

    def test_phase4_smoke_starts_without_claiming_results(self) -> None:
        smoke = (ROOT / "docs/phase4-stabilization-smoke-test.md").read_text(
            encoding="utf-8"
        )
        for check in (
            "Scope reconciliation",
            "Fixture repository",
            "Git/PR gate",
            "Issue data gate",
            "Cleanup/stale client",
            "Release gate",
        ):
            self.assertRegex(smoke, rf"\| {check} \| [^\n]+ \| not run \|")
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_docs.Phase4DocumentationTest -v
```

Expected: ERROR because `docs/phase4-stabilization-smoke-test.md` does not exist, and no test passes accidentally.

- [ ] **Step 3: Commit the RED contract**

```bash
git add tests/container/test_docs.py
git commit -m "test: define Phase 4 documentation contract"
```

### Task 2: Reconcile scope and add the Phase 4 smoke guide

**Files:**
- Modify: `docs/superpowers/specs/2026-08-22-agent-container-design.md`
- Modify: `README.md`
- Modify: `docs/phase3-github-broker.md`
- Create: `docs/phase4-stabilization-smoke-test.md`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: `Phase4DocumentationTest` from Task 1 and the approved Phase 4 spec.
- Produces: one consistent public scope and a host checklist whose initial observation rows are all `not run`.

- [ ] **Step 1: Reconcile the initial design**

Change the initial-scope family entry to state that the shipped interface is selected-repository read-only and add this explicit disposition near the Phase list:

```markdown
初期案に含めたfamily Issue create/commentは、開発repository brokerと権限を共有せず、将来Phaseのfamily専用設計へ延期する。現行interfaceは選択中repositoryのIssue list/viewだけを提供する。domain allowlist／egress controlもPhase 4には含めず、既知WARNを維持して独立した将来設計とする。
```

Do not erase the historical rationale; label the scope change and date it `2026-08-29`.

- [ ] **Step 2: Align README and the operator guide**

Add the exact phrases `family Issue create/comment` and `将来Phase` next to the current read-only boundary. State that host `gh` administration is separate from container broker operations and that failure never triggers fallback. Retain the existing `domain allowlist` warning.

- [ ] **Step 3: Create the Phase 4 smoke checklist**

Create `docs/phase4-stabilization-smoke-test.md` with these sections in order:

```markdown
# Phase 4 scope整合・残存gate・v0.4.0 smoke test

## 停止条件
## 1. Scope reconciliation
## 2. Fixture repository inventory
## 3. Local doctor and credential non-exposure
## 4. Git/PR gate
## 5. Issue data gate
## 6. Cleanup and stale client
## 7. Automated verification and review
## 8. Release gate
## 記録
```

Copy the security constraints and exact commands from the approved spec and current Phase 3 guide rather than weakening them. End with this initial table:

```markdown
| check | expected | observed | date |
| --- | --- | --- | --- |
| Scope reconciliation | initial design, README, and operator guide agree | not run | — |
| Fixture repository | private exact repository and fixtures; ruleset inventory | FAIL/PARTIAL: HTTP 403 upgrade-or-public | 2026-08-29 |
| Git/PR gate | clone/fetch/push/PR succeed; negative operations denied | not run | — |
| Issue data gate | list/view/body fixed schema; Pull Request除外; excluded sentinel absent | not run | — |
| Cleanup/stale client | runtime artifacts removed and stale client denied | not run | — |
| Release gate | tests, review, CI, changelog, final approval, v0.4.0 | not run | — |
```

- [ ] **Step 4: Run documentation tests and confirm GREEN**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_docs -v
git diff --check
```

Expected: 22 documentation tests pass and `git diff --check` exits 0.

- [ ] **Step 5: Commit the scope and checklist**

```bash
git add \
  README.md \
  docs/phase3-github-broker.md \
  docs/phase4-stabilization-smoke-test.md \
  docs/superpowers/specs/2026-08-22-agent-container-design.md
git commit -m "docs: reconcile Phase 4 scope and gates"
```

### Task 3: Create the private fixture repository and host manifest

**Files:**
- Create on host: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`
- External: `jj1xgo/agent-container-smoke`

**Interfaces:**
- Consumes: approved repository name and external-state approval boundary.
- Produces: private repository with main README, milestone, open Issue, closed Issue, open PR, and a credential-free private manifest.

- [ ] **Step 1: Perform read-only collision checks**

```bash
gh repo view jj1xgo/agent-container-smoke \
  --json nameWithOwner,visibility,defaultBranchRef,url
```

Expected for first setup: command reports that the repository does not exist. If it exists, stop and inventory it; do not overwrite, rename, delete, or repurpose it automatically.

- [ ] **Step 2: Obtain repository-creation approval**

Ask approval to create exactly `jj1xgo/agent-container-smoke` as a private repository with an initial README. Do not combine this approval with App installation, fixture creation, or release approval.

- [ ] **Step 3: Create and verify the repository once**

```bash
gh repo create jj1xgo/agent-container-smoke \
  --private \
  --add-readme \
  --description "Reusable private fixtures for agent-container broker smoke tests"

gh repo view jj1xgo/agent-container-smoke \
  --json nameWithOwner,visibility,defaultBranchRef,url
```

Expected: `nameWithOwner` is exact, visibility is `PRIVATE`, and default branch is `main`. Do not retry creation after a nonzero result until read-only inventory proves no repository was created.

- [ ] **Step 4: Obtain approval and verify GitHub App selection**

Ask approval to add the test repository to the existing GitHub App selected repositories. Do not add repository-administration operations to the broker. Create-only enforcement is local and does not require a paid ruleset or branch protection.

Verify read-only through GitHub UI or an allowlisted host query. Record repository selection and permission names/levels without credential or raw token data.

- [ ] **Step 5: Obtain fixture-creation approval**

Name every mutation: label `phase4-smoke`, milestone `phase4-excluded-field-sentinel`, one open Issue, one closed Issue, branch `fixture/pr-exclusion`, and one open PR. State that these fixtures remain for reuse.

- [ ] **Step 6: Create Issue fixtures**

```bash
gh label create phase4-smoke \
  --repo jj1xgo/agent-container-smoke \
  --color 5319e7 \
  --description "Reusable Phase 4 broker smoke fixture"

gh api --method POST \
  repos/jj1xgo/agent-container-smoke/milestones \
  -f title=phase4-excluded-field-sentinel \
  --silent

gh issue create \
  --repo jj1xgo/agent-container-smoke \
  --title "Phase 4 open Issue fixture" \
  --body "phase4-open-body-sentinel" \
  --label phase4-smoke \
  --milestone phase4-excluded-field-sentinel

gh issue create \
  --repo jj1xgo/agent-container-smoke \
  --title "Phase 4 closed Issue fixture" \
  --body "phase4-closed-body-sentinel" \
  --label phase4-smoke
```

Resolve the closed fixture with this exact query and require one result before closing it:

```bash
closed_numbers=$(gh issue list \
  --repo jj1xgo/agent-container-smoke \
  --state all \
  --json number,title \
  --jq '.[] | select(.title == "Phase 4 closed Issue fixture") | .number')
test "$(printf '%s\n' "$closed_numbers" | sed '/^$/d' | wc -l)" -eq 1
gh issue close "$closed_numbers" --repo jj1xgo/agent-container-smoke
```

If the uniqueness check fails, stop instead of guessing.

- [ ] **Step 7: Create the Pull Request exclusion fixture**

```bash
fixture_dir=$(mktemp -d /tmp/agent-container-smoke-fixture.XXXXXX)
gh repo clone jj1xgo/agent-container-smoke "$fixture_dir"
git -C "$fixture_dir" switch -c fixture/pr-exclusion
git -C "$fixture_dir" commit --allow-empty -m "test: add PR exclusion fixture"
git -C "$fixture_dir" push -u origin fixture/pr-exclusion
gh pr create \
  --repo jj1xgo/agent-container-smoke \
  --base main \
  --head fixture/pr-exclusion \
  --title "phase4-pr-exclusion-sentinel" \
  --body "Reusable open PR fixture; do not merge"
```

Keep the fixture branch and PR open. Remove only the local temporary clone after confirming it is clean and contains no unique unpushed commit.

- [ ] **Step 8: Write and validate the private fixture manifest**

Resolve the Issue and PR numbers with bounded `gh ... --json` queries. Create the parent directory at mode `0700` and write this exact schema at mode `0600`:

```json
{
  "repository": "jj1xgo/agent-container-smoke",
  "default_branch": "main",
  "open_issue": 1,
  "closed_issue": 2,
  "pull_request": 3,
  "open_body_sentinel": "phase4-open-body-sentinel",
  "closed_body_sentinel": "phase4-closed-body-sentinel",
  "excluded_field_sentinel": "phase4-excluded-field-sentinel",
  "pull_request_sentinel": "phase4-pr-exclusion-sentinel"
}
```

Replace only the three numeric examples with observed positive integers. Validate exact keys, exact repository, integer ranges, regular-file type, current-user ownership, directory `0700`, and file `0600`. Do not mount or print the manifest during the container smoke.

### Task 4: Register the dedicated broker project and run preflight

**Files:**
- External private state: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/`
- External workspace: `$AGENT_CONTAINER_HOME/workspaces/agent-container-smoke/`

**Interfaces:**
- Consumes: test repository, App selection, fixture manifest, existing App private state.
- Produces: project ID `agent-container-smoke`, exact broker policy, isolated clone, and passing Codex/Claude doctors.

- [ ] **Step 1: Verify registration preconditions read-only**

Confirm that project metadata, workspace, and handover directory do not already exist. If any exists, stop and inventory it; do not overwrite or delete it. Verify App state metadata by type, owner, and mode only.

- [ ] **Step 2: Obtain project-registration approval**

Ask approval to create the `agent-container-smoke` handover directory, clone `jj1xgo/agent-container-smoke` through the broker, and write project metadata/policy under the configured state root.

- [ ] **Step 3: Register the project**

```bash
install -d -m 700 "$AGENT_HANDOVER_ROOT/agent-container-smoke"

bin/agentctl project add jj1xgo/agent-container-smoke \
  --project agent-container-smoke \
  --handover-root "$AGENT_HANDOVER_ROOT" \
  --github-broker \
  --github-repository-id POSITIVE_INTEGER \
  --default-branch main \
  --protected-branch main
```

Expected: exact repository cloned through the broker; no legacy `gh` fallback; project policy records repository binding and `main` as default/protected with no ruleset marker.

- [ ] **Step 4: Run both doctors**

```bash
bin/agentctl doctor agent-container-smoke --github-broker
bin/agentctl doctor agent-container-smoke --agent claude --github-broker
```

Expected: every required check passes and only the documented network-policy warning remains. Remember that doctor does not prove remote App permission, paid GitHub branch settings, or network behavior.

- [ ] **Step 5: Rebuild and inspect the release-candidate image**

Run the normal latest build without fixed CLI version flags:

```bash
bin/agentctl build
podman run --rm localhost/agent-container:dev python3 -m agent_container.agentctl --version
podman run --rm localhost/agent-container:dev agent-github --help >/dev/null
podman run --rm localhost/agent-container:dev \
  stat -c '%a %U:%G %n' \
  /usr/local/bin/agent-github \
  /opt/agent-container/src/agent_container/version.py
```

Expected: build succeeds, the wrapper is executable, and Python source mode is `0644`. Record public versions only; do not record image identifiers, credential paths, or values.

### Task 5: Execute Git and Pull Request broker gates

**Files:**
- Modify external fixture repository through approved broker operations only.
- Record later in: `docs/phase4-stabilization-smoke-test.md`

**Interfaces:**
- Consumes: registered project and rebuilt image from Task 4.
- Produces: bounded Git/PR success and denial evidence plus a smoke PR number.

- [ ] **Step 1: Start one broker runtime and run credential non-exposure checks**

```bash
bin/agentctl run agent-container-smoke --github-broker
```

Inside the container, print booleans only for absence of `GH_CONFIG_DIR`, `GITHUB_TOKEN`, `GH_TOKEN`, host `.config/gh`, App private state, SSH agent, and host credential helpers. Confirm only broker socket and capability file names/types are present; do not display their contents or metadata-derived values.

Use fixed boolean probes, not `env`, `set`, `/proc/*/environ` content, or credential commands that reveal values:

```bash
for name in GH_CONFIG_DIR GITHUB_TOKEN GH_TOKEN SSH_AUTH_SOCK; do
  eval "is_set=\${$name+x}"
  printf '%s_unset=%s\n' "$name" "$(test -z "$is_set" && echo true || echo false)"
done
test ! -e "$HOME/.config/gh"; printf 'host_gh_absent=%s\n' "$?"
find /run/agent-broker -mindepth 1 -maxdepth 1 -printf '%f type=%y\n' | sort
```

The final `find` output must contain only `broker.sock type=s` and `capability type=f`.

- [ ] **Step 2: Run fetch once**

```bash
git fetch origin main
```

Expected: exit 0, `FETCH_HEAD` updated, exact broker repository, no credential in remote URL or output. Do not retry on failure.

- [ ] **Step 3: Obtain mutation approval for one smoke branch and PR**

Generate the branch name first and include its exact value in the approval request:

```bash
run_id=$(date -u +%Y%m%d-%H%M%S)
smoke_branch="test/phase4-broker-smoke-$run_id"
printf 'smoke_branch=%s\n' "$smoke_branch"
```

Name that branch, its empty commit, normal push, and unmerged PR. Also name each negative push shape and state that all targets are inside the disposable repository.

- [ ] **Step 4: Create and push the normal work branch**

```bash
git switch -c "$smoke_branch"
git commit --allow-empty -m "test: Phase 4 broker smoke"
git push -u origin "$smoke_branch"
```

Expected: each command exits 0 and audit records one allowed `git-receive-pack` without commit content.

- [ ] **Step 5: Execute exact protected, delete, and non-head denials**

Set `smoke_branch` to the approved unique branch name, then run each command once:

```bash
git push origin HEAD:refs/heads/main
git push origin ":refs/heads/$smoke_branch"
git push origin "HEAD:refs/tags/phase4-broker-smoke-denied-$run_id"
```

Expected: protected main, delete, and non-head ref each fail before mutation. Capture exit code and allowlisted audit `{timestamp,operation,status,stage,ref}` only. Stop immediately if any forbidden mutation succeeds; do not attempt automatic restoration.

- [ ] **Step 6: Verify ordinary descendant fast-forward denial**

Advance the local branch by one descendant commit, then attempt one ordinary
push without `--force`:

```bash
git commit --allow-empty -m "test: Phase 4 create-only fast-forward denial"
git push origin "$smoke_branch"
```

Expected: the broker rejects this advertised-branch fast-forward before the
GitHub receive-pack RPC, the remote smoke branch remains unchanged, and audit
records `git-receive-pack` as denied without object IDs or commit content.

- [ ] **Step 7: Verify unrelated-history non-fast-forward denial**

Create an unrelated commit object without changing the checkout, then attempt one forced update of only the disposable smoke branch:

```bash
tree_oid=$(git rev-parse 'HEAD^{tree}')
unrelated_oid=$(printf '%s\n' 'test: unrelated Phase 4 history' | git commit-tree "$tree_oid")
git push --force origin "$unrelated_oid:refs/heads/$smoke_branch"
```

Expected after the create-only correction: the broker rejects this distinct
unrelated-history non-fast-forward update before the GitHub receive-pack RPC;
the remote smoke branch remains unchanged and audit records
`git-receive-pack` as denied without object IDs or commit content. Together
with Step 6, this separately verifies both update shapes. Do not print object
contents or pack data.

- [ ] **Step 8: Spike deterministic stale-lease synchronization**

The feasibility question is: can the existing helper be paused after receive-pack advertisement and before its RPC while a host-admin update advances only the disposable smoke branch, without copying or displaying capability data? Inspect the current helper and broker protocol, and propose one synchronization point that requires no production backdoor. Obtain separate approval before the host-admin ref update.

If a deterministic method exists, execute it once and require the broker's advertised-old-OID gate to deny the stale request. If no deterministic method exists without a production test hook or timing race, do not simulate success: retain the automated stale-lease tests as mandatory evidence and record the real-host row `PARTIAL` with that reason.

- [ ] **Step 9: Create and inspect the smoke PR**

```bash
pr_json=$(agent-github pr create \
  --base main \
  --head "$smoke_branch" \
  --title "test: Phase 4 broker smoke" \
  --body "Phase 4 approved disposable-repository smoke")

pr_number=$(printf '%s\n' "$pr_json" | jq -er '.number')
printf '%s\n' "$pr_json"

agent-github pr view "$pr_number"
agent-github pr checks "$pr_number"
```

Require `pr_number` to come only from the bounded create response. Confirm merge, close, release, workflow dispatch, repository administration, and generic API are absent from the client parser. Do not merge the smoke PR.

### Task 6: Execute Issue data, cleanup, and stale-client gates

**Files:**
- Read host-only: `$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json`
- Record later in: `docs/phase4-stabilization-smoke-test.md`

**Interfaces:**
- Consumes: manifest identities and the same broker runtime used for Task 5.
- Produces: nonempty list/view evidence, PR exclusion, fixed-field evidence, cleanup evidence, and stale-client denial.

- [ ] **Step 1: Select fixture numbers outside the container**

Read only the manifest's integer identifiers into shell variables without printing the manifest:

```bash
manifest="$AGENT_CONTAINER_HOME/projects/agent-container-smoke/smoke-fixtures.json"
test "$(stat -c '%F:%a:%u' "$manifest")" = "regular file:600:$(id -u)"
test "$(jq -r '.repository' "$manifest")" = "jj1xgo/agent-container-smoke"
test "$(jq -r 'keys | sort | join(",")' "$manifest")" = \
  "closed_body_sentinel,closed_issue,default_branch,excluded_field_sentinel,open_body_sentinel,open_issue,pull_request,pull_request_sentinel,repository"
open_issue=$(jq -er '.open_issue | select(type == "number" and . >= 1 and . <= 2147483647)' "$manifest")
closed_issue=$(jq -er '.closed_issue | select(type == "number" and . >= 1 and . <= 2147483647)' "$manifest")
```

Pass only these numbers as command arguments; never mount or print the manifest.

- [ ] **Step 2: Run Issue list once**

```bash
agent-github issue list
```

Expected: fixed top-level `issues` array contains the open Issue, excludes the closed Issue, excludes the Pull Request and `phase4-pr-exclusion-sentinel`, and contains no `phase4-excluded-field-sentinel`.

- [ ] **Step 3: View open and closed fixtures once each**

```bash
agent-github issue view "$open_issue"
agent-github issue view "$closed_issue"
```

Expected: each fixed object contains exactly `number`, `title`, `state`, `author`, `labels`, `created_at`, `updated_at`, `url`, and `body`; state and body sentinel match the manifest; excluded milestone sentinel is absent.

- [ ] **Step 4: Verify unavailable Issue interfaces locally**

Run create, edit, comment, close, search, query, pagination, generic API, repository override, zero, negative, and non-integer forms. Each must exit 2 before requester invocation, with empty stdout and argparse-only stderr. Do not substitute host `gh` for these denial checks.

- [ ] **Step 5: Prepare the stale client without exposing capability**

While the runtime is active, use a second host terminal to resolve the single active run directory by type/name only, then prepare a blocked host-side client:

```bash
stale_tmp=$(mktemp -d /tmp/agent-github-stale.XXXXXX)
mkfifo "$stale_tmp/go"
run_dirs=$(find "$AGENT_CONTAINER_HOME/github-broker/r" \
  -mindepth 1 -maxdepth 1 -type d -print)
test "$(printf '%s\n' "$run_dirs" | sed '/^$/d' | wc -l)" -eq 1
run_dir=$run_dirs
test -S "$run_dir/broker.sock"
test -f "$run_dir/capability"

(
  read -r _ <"$stale_tmp/go"
  env \
    AGENT_BROKER_SOCKET="$run_dir/broker.sock" \
    AGENT_BROKER_CAPABILITY="$run_dir/capability" \
    AGENT_BROKER_REPOSITORY=jj1xgo/agent-container-smoke \
    AGENT_PROJECT_ID=agent-container-smoke \
    PYTHONPATH="$PWD/src" \
    python3 -m agent_container.github_client issue list \
      >"$stale_tmp/stdout" 2>"$stale_tmp/stderr"
  printf '%s\n' "$?" >"$stale_tmp/code"
) &
stale_pid=$!
```

Do not copy, print, stat, hash, or encode the capability. Exit the agent runtime normally. Then, from the host terminal, signal and inspect only bounded results:

```bash
printf 'go\n' >"$stale_tmp/go"
wait "$stale_pid"
test "$(cat "$stale_tmp/code")" -ne 0
test ! -s "$stale_tmp/stdout"
test "$(cat "$stale_tmp/stderr")" = "error: GitHub broker request failed"
```

Require no new successful audit record. After verification, remove only this exact `$stale_tmp` directory.

- [ ] **Step 6: Verify cleanup and audit**

On the host, confirm no socket or file named `capability` remains under the completed run directory. Query audit with an explicit allowlist:

```bash
jq -c \
  'select(.project=="agent-container-smoke")
   | {timestamp,operation,status,stage,repository,ref,pr_number,issue_number,bytes,policy_version}' \
  "$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl"
```

Confirm no unexpected keys by separately comparing `keys | sort` with the operation-specific allowlist. Do not print the whole audit file.

- [ ] **Step 7: Obtain cleanup approval and remove only the smoke branch**

After recording the unmerged smoke PR state, ask approval to close exactly `$pr_number` through host administration and delete only `$smoke_branch`. Expand both values in the approval message. Keep the fixture PR, Issues, label, milestone, repository, project state, and fixture manifest.

### Task 7: Record observations and prepare v0.4.0

**Files:**
- Modify: `docs/phase4-stabilization-smoke-test.md`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: bounded evidence from Tasks 3-6.
- Produces: evidence-backed smoke table, release notes, and tests that reject unrecorded or overstated status.

- [ ] **Step 1: Tighten the docs test to the observed statuses**

Replace the initial `not run` expectations with `(check, status)` pairs matching actual evidence. Use a regex that binds each check label to its status without freezing the whole prose row. Add assertions for the date, fixture repository, smoke PR number, and `v0.4.0`.

- [ ] **Step 2: Run the focused test and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_docs.Phase4DocumentationTest -v
```

Expected: FAIL because the smoke rows and release notes still contain pre-execution state.

- [ ] **Step 3: Record only observed results**

Update each row with `PASS`, `PARTIAL`, or `not run`, exact date, bounded identifiers, and a reason for every non-PASS. Separate intermediate failures from the final rerun. Do not include credential-derived data, raw audit lines, Issue body beyond named test sentinels, or environment output.

- [ ] **Step 4: Prepare release notes**

Set `release_date=$(date -u +%F)` and move Unreleased content into a header containing that observed value, for example on 2026-08-29:

```markdown
## [0.4.0] - 2026-08-29
```

Summarize Issue read-only support, Git 2.53 compatibility, Phase 4 scope reconciliation, dedicated broker gates, and known limitations. Keep family Issue write and domain allowlist explicitly deferred. Update `Current release` in README only in the release PR, immediately before release.

- [ ] **Step 5: Run focused and full verification**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_docs -v
PYTHONPATH=src python3 -m unittest discover -s tests -v
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 \
  PYTHONPATH=src python3 -m unittest \
    tests.integration.test_github_broker_socket \
    tests.integration.test_handover_broker_socket -v
git diff --check
```

Expected: every test passes; only explicitly gated Podman integration may remain skipped in the ordinary full-suite command.

- [ ] **Step 6: Run real Podman integration**

Run:

```bash
AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 \
AGENT_CONTAINER_INTEGRATION_BASE_IMAGE=localhost/agent-container:dev \
PYTHONPATH=src python3 -m unittest \
  tests.integration.test_project_image_podman -v
```

Expected: pass, and only exact disposable test images are removed afterward.

- [ ] **Step 7: Commit release documentation**

```bash
git add README.md CHANGELOG.md docs/phase4-stabilization-smoke-test.md tests/container/test_docs.py
git commit -m "docs: record Phase 4 stabilization gates"
```

### Task 8: Review, merge, and verify the release candidate

**Files:**
- Review all files changed since `origin/main`.
- External: GitHub pull request and CI.

**Interfaces:**
- Consumes: Tasks 1-7 commits and fresh verification.
- Produces: reviewed release PR merged to `main`, with a known merge commit.

- [ ] **Step 1: Request independent review**

Use `superpowers:requesting-code-review`. Require explicit findings for scope contradictions, credential exposure, host/broker fallback confusion, unsafe external mutations, fixture reuse, stale-client validity, negative-push safety, overclaimed smoke status, release rollback, and tag immutability. Fix all Critical and Important findings; evaluate Minor findings technically.

- [ ] **Step 2: Re-run verification after review fixes**

Run the complete commands from Task 7 Steps 5-6 plus `git diff --check`. Read the full result and record exact pass/skip counts.

- [ ] **Step 3: Obtain push and PR approval**

Name the branch, HEAD commit, remote repository, PR base `main`, purpose, verification, and external-state effects. Push only after explicit approval. Create the PR using the project-scoped broker when available; do not silently fall back.

- [ ] **Step 4: Wait for CI and resolve failures**

Require Unit tests and Podman integration to pass. For a failure, inspect the failed job read-only, reproduce locally when possible, and use systematic debugging before modifying code or docs.

- [ ] **Step 5: Obtain merge approval and merge**

Present PR URL, review result, CI result, host-gate summary, and residual risk. Merge only after explicit approval. Fetch `origin/main` and verify the release candidate commit is an ancestor of the resulting merge commit.

### Task 9: Create and verify v0.4.0

**Files:**
- External: Git tag `v0.4.0` and GitHub Release.

**Interfaces:**
- Consumes: clean `origin/main` merge commit from Task 8 and successful release gates.
- Produces: immutable annotated tag and published GitHub Release pointing to the same commit.

- [ ] **Step 1: Perform read-only release inventory**

```bash
git fetch origin main --tags
git status --short --branch
git tag --list v0.4.0
gh release view v0.4.0 --repo jj1xgo/agent-container
```

Expected: clean main at exact `origin/main`; neither tag nor release exists. If either exists, stop and do not move, delete, overwrite, or reuse it.

- [ ] **Step 2: Verify the exact release commit and local tag version**

Run the full automated suite on clean main and confirm both CI jobs for the merge commit. Then create the still-unpublished annotated tag locally and verify the version:

```bash
release_commit=$(git rev-parse origin/main)
git tag -a v0.4.0 "$release_commit" -m "v0.4.0"
test "$(PYTHONPATH=src python3 -m agent_container.agentctl --version)" = \
  "agentctl 0.4.0"
test "$(git rev-list -n 1 v0.4.0)" = "$release_commit"
```

If verification fails, delete only this unpushed local tag and stop. Do not push it or create a Release.

- [ ] **Step 3: Obtain final release approval**

State the exact commit SHA, tag `v0.4.0`, repository, release title, release notes source, CI URLs, host-gate status, remaining family Issue write deferral, and network-policy WARN. Ask separately for permission to create and push the annotated tag and publish the GitHub Release.

- [ ] **Step 4: Push the verified annotated tag once**

```bash
test "$(git rev-list -n 1 v0.4.0)" = "$release_commit"
git push origin refs/tags/v0.4.0
```

Require `release_commit` to equal the full SHA named in the approval. Do not retry after ambiguous output until read-only remote tag inventory determines whether the push succeeded.

- [ ] **Step 5: Publish the GitHub Release once**

```bash
gh release create v0.4.0 \
  --repo jj1xgo/agent-container \
  --title "v0.4.0" \
  --verify-tag \
  --notes-from-tag
```

Do not retry after ambiguous output until `gh release view` proves whether the release exists.

- [ ] **Step 6: Verify published state read-only**

Verify the remote tag object, peeled commit, GitHub Release tag, URL, published state, and main CI result. Confirm the tag points to the exact approved `origin/main` commit. Report the retained test repository/fixtures and the two deferred future designs.
