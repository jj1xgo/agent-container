# Project-scoped GitHub repository binding design

Date: 2026-08-29

## Context

The GitHub broker keeps one host-private App identity in
`$AGENT_CONTAINER_HOME/github-broker/app.json` and `private-key.pem`. The App
installation may select multiple repositories, but the current metadata also
contains one `repository_id`. Every installation token request is narrowed to
that single ID.

Phase 4 attempted to register `jj1xgo/agent-container-smoke` while preserving
the existing production repository selection. Token issuance succeeded, but
the broker's read-only Git discovery failed at `upload-discovery`. Bounded host
verification established that the global metadata repository ID does not match
the smoke repository. The broker therefore obtained a valid single-repository
token for a different selected repository and could not read the repository in
the smoke project policy.

Changing the global repository ID would move, rather than solve, the failure:
the existing production project would lose its token binding. Duplicating the
private key per project would unnecessarily expand secret storage and rotation
scope.

## Goal

Allow multiple broker projects to share one GitHub App identity and private key
while each installation token remains restricted to exactly the repository
bound to that project.

## Non-goals

- Do not add a generic GitHub API, repository administration, App installation
  administration, credential export, or token inspection interface.
- Do not broaden Git, Pull Request, Checks, or Issue permissions.
- Do not put repository IDs, tokens, JWTs, private keys, or capabilities in
  broker audit records or container mounts.
- Do not duplicate the App private key into project directories.
- Do not remove support for existing broker projects or require an immediate
  destructive migration.
- Do not retry the failed host registration until implementation, tests,
  review, bounded partial-state verification, and a new mutation approval are
  complete.

## Architecture

The global App metadata remains the source of `client_id`, `installation_id`,
and the private-key path. Each project broker policy becomes the source of the
repository slug and its positive integer GitHub repository ID.

At runtime the broker loads both records, validates the project policy, and
constructs token metadata from the global App identity plus the project-bound
repository ID. `InstallationTokenProvider` continues requesting the exact
minimal permission map and exactly one repository. Its existing response
validation continues requiring that the returned repository is the requested
ID.

```text
global App client/installation/private key
                    +
project repository slug/repository ID
                    |
                    v
one-repository minimal-permission installation token
                    |
                    v
existing project-scoped broker transport
```

The public broker protocol, Git remote URL, runtime socket/capability mounts,
and Git/PR/Issue commands do not change.

## CLI contract

Broker project registration accepts:

```text
agentctl project add OWNER/REPOSITORY \
  --github-broker \
  --github-repository-id POSITIVE_INTEGER \
  ...
```

`--github-repository-id` is valid only with `--github-broker`. A new broker
project requires it. Boolean values, strings that are not canonical decimal
integers, zero, and negative values are rejected before external mutation.

Non-broker project registration is unchanged.

## Policy schema and compatibility

New `$AGENT_CONTAINER_HOME/projects/PROJECT/github-broker.json` records contain
the existing exact fields plus:

```json
{
  "repository": "OWNER/REPOSITORY",
  "repository_id": 123,
  "default_branch": "main",
  "protected_branches": ["main"],
  "ruleset_confirmed": true
}
```

The numeric value above is illustrative only. Runtime validation requires a
positive non-boolean integer and rejects unknown fields, duplicates, symlinks,
wrong ownership, or modes other than `0600`.

Legacy policies with the exact old field set remain readable. For them only,
runtime token construction falls back to the global App metadata repository
ID. This preserves the behavior of an existing single-repository production
project. Newly written policies always contain `repository_id`; the CLI never
creates another legacy policy.

The local `doctor` distinguishes an explicit project binding from a legacy
global fallback. It does not claim to verify remote App selection, permission,
repository identity, or ruleset state.

## Interrupted-registration recovery

The failed smoke registration left a mode `0700` project directory, a mode
`0600` legacy-schema broker policy, the previously approved fixture manifest,
and a mode `0700` handover directory. It did not create `project.json` or a
workspace.

`project add` may upgrade an existing legacy policy during registration only
when all of these conditions hold:

1. `--github-repository-id` is explicitly supplied.
2. The existing policy's repository, default branch, protected branches, and
   ruleset confirmation exactly match the requested values.
3. `project.json` and the project workspace are both absent and are not
   symlinks.
4. The existing policy is a current-user-owned regular non-symlink file at
   mode `0600`.

The upgrade adds only the validated repository ID. It uses a same-directory
temporary regular file, `fsync`, mode `0600`, and atomic replacement after all
checks pass. Existing sibling files, including `smoke-fixtures.json`, remain
unchanged.

If any condition fails, registration stops without changing the policy. An
explicit ID that differs from an already project-bound policy is always an
error. A completed project cannot be rebound through `project add`.

## Failure handling

- Policy/repository/ID mismatches fail before broker startup or Git network
  access.
- Malformed or unsafe policy state fails closed without repair.
- A token response that does not contain exactly the requested repository and
  permission map remains invalid.
- Broker audit continues recording only its current allowlisted fields. The
  repository ID is not added.
- A failed GitHub mutation or registration is not automatically retried.
  Recovery requires diagnosis, bounded state inventory, and fresh approval.

## Testing

Implementation follows test-driven development and covers:

1. CLI parsing and rejection outside `--github-broker`.
2. Strict positive-integer validation.
3. New-policy serialization and strict loading.
4. Legacy-policy loading and global-ID fallback.
5. Runtime token construction using different repository IDs for two projects
   sharing one App identity/private key.
6. Existing production-project compatibility.
7. Exact interrupted-state upgrade while preserving sibling files.
8. Refusal to change a mismatched policy, completed project, existing
   workspace, symlink, wrong owner/mode, malformed schema, or already-bound
   different ID.
9. No repository ID added to audit or container mounts.
10. Existing Git/PR/Checks/Issue broker regression suites.

Focused tests run first, followed by the complete unit/integration suite and
`git diff --check`. Host recovery occurs only after code review and fresh
read-only inventory. The host smoke then verifies both Codex and Claude doctors
and confirms that both production and smoke projects retain their independent
repository bindings.

## Documentation impact

Update the Phase 3 operator guide, Phase 4 smoke guide, README, and CHANGELOG to
describe the project-scoped repository binding, the legacy fallback, the local
scope of doctor results, and the approval boundary for interrupted-state
recovery. The original Phase 4 evidence must retain the observed
`upload-discovery` failure and its diagnosed cause rather than rewriting it as
an initial success.
