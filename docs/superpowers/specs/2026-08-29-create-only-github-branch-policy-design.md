# Create-only GitHub branch policy design

Date: 2026-08-29

## Context

The GitHub broker currently rejects protected refs, deletions, non-head refs,
stale leases, malformed receive-pack commands, and unsupported capabilities
before forwarding a push to GitHub. It delegates non-fast-forward rejection to
an active GitHub ruleset covering every branch because a receive-pack update
command contains old and new object IDs but does not state whether the new
commit descends from the old commit.

The Phase 4 private smoke repository is on a GitHub plan that does not provide
rulesets or protected branches for private repositories. The ruleset inventory
request returned HTTP 403 with an explicit upgrade-or-public requirement, and
the approved negative smoke demonstrated that an unrelated-history force push
to the disposable test branch succeeded. The remote branch therefore changed,
and the gate stopped before its final read-only OID check.

The broker must enforce its advertised push safety without relying on an
unavailable paid GitHub feature or processing an untrusted Git object graph on
the host.

## Goal

Make broker-mediated branch pushes safe on private repositories without paid
GitHub branch rules by allowing creation of a new work branch and rejecting
every update to a branch that already exists remotely.

## Non-goals

- Do not allow fast-forward updates to an existing remote branch.
- Do not parse packfiles, commits, trees, deltas, or other untrusted Git
  objects on the host.
- Do not stage objects or refs in temporary GitHub branches.
- Do not add repository-administration permissions or generic GitHub API
  access.
- Do not make the smoke repository public, change its subscription, or modify
  GitHub repository settings.
- Do not restore the mutated smoke branch as part of implementation.
- Do not merge, release, or change the production repository.

## Security model

The server receive-pack advertisement is the broker's authoritative snapshot
for deciding whether a requested ref already exists. For every update command,
the broker applies these rules before invoking the GitHub receive-pack RPC:

1. The ref must remain under `refs/heads/` and must not be protected.
2. The new object ID must not be the zero object ID, preserving deletion
   rejection.
3. The ref must be absent from the advertisement.
4. The old object ID must be the zero object ID.

A command for an advertised ref is rejected even when its old object ID
exactly matches the advertisement. This deliberately rejects both
fast-forward and non-fast-forward updates. A command for an absent ref with a
nonzero old object ID remains a stale-lease failure. If any command in a
multi-ref request fails, the entire request is rejected before the packfile is
sent to GitHub.

There is an unavoidable interval between advertisement and receive-pack RPC.
If another client creates the same branch during that interval, GitHub's
ordinary old-zero ref creation check rejects the request. The broker therefore
fails safely without needing a lock or retry.

The broker does not infer ancestry from object IDs and does not trust the
container to report ancestry. It accepts the workflow limitation that a pull
request branch cannot receive subsequent broker-mediated pushes. Additional
work uses a new branch and, when needed, a new pull request.

## Receive-pack data flow

```text
GitHub receive advertisement
          |
          v
parse advertised refs and object format
          |
          v
container receive-pack command section
          |
          v
existing ref/ref type/protected/delete/lease validation
          +
create-only check: ref absent and old OID zero
          |
     allowed only
          v
forward unchanged request to exact GitHub receive-pack endpoint
```

No public broker protocol fields change. The container continues receiving the
real GitHub advertisement and sending the standard bounded receive-pack
request. The host still forwards the request unchanged after all command gates
pass.

## Policy schema and CLI compatibility

Create-only behavior is an invariant of the broker implementation, not a
user-asserted policy option. Newly written project policies use the exact
field set:

```json
{
  "repository": "OWNER/REPOSITORY",
  "repository_id": 123,
  "default_branch": "main",
  "protected_branches": ["main"]
}
```

The repository ID is illustrative. Existing validation of its positive
non-boolean integer value and private file boundary remains unchanged.

Existing exact policy schemas containing `"ruleset_confirmed": true` remain
readable so deployed production and smoke projects do not require an automatic
filesystem mutation. The field is treated only as a legacy schema marker; it
does not weaken, enable, or configure create-only enforcement. A false value,
unknown field, duplicate key, unsafe file, or otherwise malformed schema still
fails closed. Policy writes and interrupted-registration upgrades emit only the
new schema.

`agentctl project add --github-broker` no longer requires
`--confirm-force-push-ruleset`. The obsolete option is removed rather than
silently ignored, so an old registration command fails visibly instead of
claiming that an unavailable remote control was verified. Repository ID,
default branch, protected branch, and project binding requirements remain.

Local doctor output continues describing repository binding validity only. It
does not claim that GitHub settings or network behavior were checked.

## Failure handling and audit

A create-only violation uses the existing local receive-pack denial path. The
broker does not call the GitHub receive-pack RPC, does not retry, and records a
secret-free audit entry with operation `git-receive-pack` and status `denied`.
It does not record old or new object IDs, pack contents, capabilities, commit
messages, or file content.

Discovery, framing, GitHub RPC, and response-stream failures retain their
existing bounded stage classifications. A race in which GitHub rejects a
newly occupied ref remains a `receive-rpc` error because it occurs after the
local create-only gate; there is no automatic rediscovery or retry.

## Testing

Implementation follows test-driven development and covers:

1. A new unprotected branch with an absent advertised ref and zero old object
   ID reaches the receive-pack transport.
2. An advertised work branch is denied before RPC even when the old object ID
   matches, covering a would-be fast-forward update.
3. An advertised work branch with unrelated new history is denied by the same
   invariant, covering force-push behavior without parsing a packfile.
4. A multi-ref request containing one existing ref is denied as a whole and
   sends no RPC.
5. An absent ref with a nonzero old object ID remains denied.
6. Protected branch, delete, tag, malformed command, unsupported capability,
   and ref-count/size bounds remain denied.
7. New project policy serialization omits `ruleset_confirmed` and reloads
   strictly.
8. Both legacy project-scoped and legacy global-binding policies with an exact
   true ruleset marker remain readable and receive create-only enforcement.
9. False legacy markers and mixed or unknown schemas remain rejected.
10. CLI registration succeeds without the obsolete confirmation flag and the
    removed flag is not accepted.
11. Audit output for a create-only denial contains no object IDs or request
    body data.
12. Existing upload-pack, PR, Checks, Issue, runtime isolation, and recovery
    suites remain green.

Run focused protocol, transport, runtime, policy, CLI, and documentation tests
first. Then run Ruff 0.16.4, the complete test suite, and `git diff --check`.
Host verification happens only after review, a new image build, and explicit
approval for each external mutation.

## Documentation and Phase 4 evidence

Update the Phase 3 design/operator/smoke documentation, Phase 4 release design,
plan, smoke guide, README, and CHANGELOG so they describe create-only branches
and no longer instruct users to confirm a paid ruleset. Preserve historical
evidence as observations: the unavailable ruleset check returned HTTP 403, the
force push succeeded, and the disposable remote branch changed.

The corrected negative host gate must use a newly approved disposable branch
or an explicitly approved restoration of the current branch. It verifies one
new-branch push followed by denial of every subsequent update. PR, Issue,
cleanup, and release gates remain stopped until this implementation and its
host verification pass.
