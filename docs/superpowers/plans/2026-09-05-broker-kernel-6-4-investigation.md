# Phase 6-4 GitHub broker: implementation planning inputs

Status: option 1 approved by the user on 2026-09-05. Implementation plan: `2026-09-05-broker-kernel-6-4-github.md`; production implementation has not started.

## Source versions

- GitHub implementation and tests: `1924f71560e2a92abd97667fc52853aea7cc946e`.
- Kernel inspected at PR #95 head
  `e3abc9ffac9c2f305566d334bf0cde85b3da65b1`; subsequently updated to
  `86fefc1e7dcc6c5bdd178e18f5088e3f28db297b` by merging main. Only AGENTS.md
  and docs/codex-operations.md changed in that update; inspected code is identical.
- Specification: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`.
- PR #95 merged as `a69bb780dc61f3f0f50c92f668f8686837280f12`. Required
  Unit tests and Podman integration passed on `86fefc1`; its tree equals the
  merge result (`git diff --exit-code 86fefc1 a69bb78` passed).
- The pre-existing untracked 6-3 plan was one final review note behind the PR
  version. Its bytes are preserved at
  `.worktrees/resume-backups/2026-09-05-broker-kernel-6-3-egress.pre-resume.md`.
- Local main was fast-forwarded to `a69bb78`; PR #95 remote/local branches and
  its clean worktree were removed. The pre-existing `.git/config.lock` remains;
  local branch deletion succeeded but its config update emitted a lock warning.
  Other worktrees and launcher dotfiles were retained.

## Scope and acceptance

Stage 1 preserves wire bytes, audit lines, errors, lifecycle, permissions,
existing tests, StateLayout, Mount classes, and `podman.py`. GitHub operation
handlers and policy remain broker specific. Existing bugs belong in separate
changes. A passing existing suite is insufficient to prove preservation of
previously untested behavior.

## Verified compatibility differences

| Surface | Current GitHub behavior | Kernel behavior / implication |
| --- | --- | --- |
| Request encode | UTF-8, insertion order, `ensure_ascii=False`, `allow_nan=False`; serialization errors escape | `encode_frame` converts TypeError, UnicodeEncodeError and ValueError to fixed ValueError |
| Request decode | Exact fields, duplicate and nonstandard constants rejected; version is any non-bool int, authorization checks version later | Frame decoding can be shared; keep typed validation and authorization placement |
| Response encode | Sorted ASCII JSON; `version=True` passes equality with 1 | Preserve equality semantics; do not add bool rejection incidentally |
| Response header | Fewer than 4 bytes gives `broker response frame is incomplete`; invalid size or truncated body gives `broker response frame is invalid` | Kernel distinguishes incomplete body and invalid size |
| Response duplicate key | `ValueError("broker request JSON is invalid")`, including nested duplicates | Kernel reports response JSON error |
| Response constants | JSON accepts constants, then schema/status checks apply | Kernel rejects constants before schema |
| Response status type | A list/dict status can raise TypeError on set membership | Do not silently normalize the exception |
| Stream reads | Raw OSError escapes; initial clean EOF optionally terminates chunk iteration | Kernel wraps read errors as ValueError and currently lacks initial EOF option |
| Chunk writes | One write for header, one for payload, final zero header then flush; ignores short-write return values | Replacing with `write_all` repairs an existing bug and changes behavior |
| Client capability | Pre-open path/stat/mode/owner checks, size ceiling 45, reads 46 bytes, validates exact capability plus newline | Kernel adds descriptor identity checks, O_NONBLOCK, and exact stat size 44 |
| Egress capability | 0400/0444 accepted; 0600 rejected; lacks GitHub/kernel owner and size checks | Do not share the strict kernel reader without an explicit design change |
| Accepted GitHub client | `with client`, makefile rwb/unbuffered, closes stream in finally; no settimeout or SO_PEERCRED | Use raw-client seam if adopting runtime; `open_connection` would add behavior |
| Runtime startup | Catches Exception, session.close, re-raises original error; cleanup may replace error | Kernel catches BaseException, performs additional cleanup and wraps failures |
| Runtime serve | Errors after stop are suppressed, including handler/stream close failures | Kernel records such failures; raw-client seam alone is insufficient |
| Runtime stop | stop flag, listener close, thread join 2 seconds, session.close, then fixed failure; no deactivate | Kernel wraps cleanup errors, has exited state, and runs deactivate callbacks |
| Session cleanup | Marks closed and clears capability first; socket then capability then rmdir; fail-fast; repeat close is no-op | Kernel removes capability before socket, accumulates failures, returns bool |
| Capability creation | UTF-8 TextIO, mode from open subject to umask, no fchmod | Kernel ASCII encoding and fchmod add observable behavior |
| Audit open | Symlink precheck; fstat mode/owner; returns TextIO; original open errors | Kernel O_NONBLOCK and inode recheck add guarantees and alter error mapping |
| Audit record | Insertion order, optional ref/pr_number/issue_number/stage, ASCII escapes, newline, fsync | Preserve actual code keys and ordering; specification's short key list is not exhaustive |

## Reproductions on current main

Synthetic inputs only; no credentials or external operations were used.

| Probe | Observed result |
| --- | --- |
| Response duplicate version | ValueError: broker request JSON is invalid |
| Response `version: Infinity` | ValueError: broker response schema is invalid |
| Response `version: true, status: ok` | Accepted BrokerResponse(version=True, status='ok') |
| Response `status: []` | TypeError |
| Request payload containing object() | TypeError from JSON serialization |
| GitHub stream read raises OSError | Same OSError propagated |
| Kernel read_exact on same failing stream | ValueError: broker stream is invalid |
| Chunk writer accepts 1 byte per write, input `ab` | Reports 2 transferred bytes but wire is hex `006100` |

These probes characterize existing behavior; they do not approve or repair it.

## Preserved private surface inventory

Searched **all of `tests/`**, including integration, using
`rg -n 'runtime\._|session\._' tests`, then searched class names, `_serve`,
`_thread`, `_stop`, `_error`, and patch targets to catch aliases.

| Name | Existing consumer |
| --- | --- |
| BrokerSession._closed | `tests/container/test_github_broker.py:31` |
| BrokerSession._capability | `tests/container/test_github_broker.py:38,80,138,191`; `test_github_broker_transport.py:386` |
| BrokerSession._seen_sequences | `tests/container/test_github_broker.py:95` |
| BrokerSession._listener | Production consumer `UploadPackBrokerRuntime.__exit__`; preserve until that caller is safely migrated |
| github_broker_runtime._validate_policy_parent_identity | `tests/container/test_github_broker_runtime.py:464`; policy upgrade is out of scope |
| github_broker.socket.socket / os.chmod | Existing broker tests patch these module paths; retain working patch seams |
| github_broker_transport.read_broker_capability / validate_broker_socket | Existing transport client tests patch these module paths |
| UploadPackBrokerRuntime.create and transport constructors | Runtime factory tests and `tests/container/test_agentctl.py` |

No GitHub-specific direct reads/writes of runtime `_thread`, `_stop`, `_error`
or calls to `_serve` were found in the current tests. They still exist in
production and require a deliberate preservation decision, rather than removal
based solely on lack of existing tests. GitHub socket integration constructs
UploadPackBrokerRuntime and exercises its context-manager lifecycle.

## Options for the implementation plan

1. **Preserve thin compatibility code in GitHub (recommended).** Share pieces
   whose contracts match, move chunk framing without repairing short writes,
   and retain local exception/lifecycle/capability adapters where needed.
   Record every deferred part in the specification. Full runtime/audit/client
   unification may therefore require a stage 1 scope amendment; do not claim
   full unification is complete when compatibility code still owns it.
2. **Add compatibility configuration to the kernel.** Preserve existing defaults
   for handover/egress and expose enough controls for GitHub exception, stream,
   audit and cleanup semantics. This gives broader sharing at the cost of a
   larger API and more interactions to verify. Merely passing raw_client=True
   and a no-op deactivate does not preserve the existing runtime.

Do not select stricter capability validation or repair short writes as an
ordinary refactoring detail. These would change the agreed stage 1 contract.

## Provisional execution sequence for option 1

Option 1 was approved on 2026-09-05. The specification and roadmap now record the reduced stage 1 scope; this sequence is retained as the investigation history.

1. Freeze the old GitHub source commit and add characterization/golden tests in
   new `tests/container/test_broker_github_golden.py` and
   `tests/container/test_github_broker_compatibility.py`. Cover wire bytes,
   optional audit fields, raw exception type/message, short reads/writes,
   startup failure and cleanup order before replacing any implementation.
2. Amend the 6-4 specification paragraph to enumerate the exact compatibility
   portions retained below. Keep stage 1 completion explicitly conditional on
   the accepted revised scope; do not silently defer its mandatory guarantees.
3. Share the matching request decode logic through FrameSchema while leaving
   GitHub typed validation in place. Retain response decode and request JSON
   serialization if their exact error mapping would require larger kernel API
   changes. Extract chunk framing only with raw read-error, clean EOF and
   current short-write behavior preserved and directly tested.
4. Share resource helpers only after their syscall and failure paths match:
   allocation and capability generation are candidates. Preserve capability
   file creation, fail-fast socket-first cleanup and the legacy audit opener
   where their contracts differ. Audit serialization/writing must retain
   opening order, optional keys and fsync completion, not just parsed JSON.
5. Reuse the kernel accept loop only through a documented compatibility seam.
   Keep GitHub client stream setup (no peer lookup or timeout) and original
   startup/shutdown exception behavior in the adapter. Do not claim complete
   lifecycle extraction merely because raw_client is enabled.
6. Keep github_broker_transport.py and egress_adapter.py capability readers
   unchanged. Record stronger validation and short-write repair as separate
   future behavior changes, without implementing them in this PR.
7. Validate against the existing unmodified suites and mandatory CI below;
   review the actual diff against every preserved surface and exception case.
   Update CHANGELOG/spec with the exact extracted and retained portions.

Expanded into writing-plans tasks with final helper signatures and executable
test code in `2026-09-05-broker-kernel-6-4-github.md` after scope approval.

## Required execution evidence

- Generate golden request/response/chunk/audit bytes from the original GitHub
  implementation at a pinned commit, not from the replacement encoder.
- Golden operations: upload-pack, receive-pack, pr-create/view/checks,
  issue-list/view; response statuses ok/denied/error; optional audit fields.
- Add meaningful characterization of the table above before migration;
  compare against the pinned original, including error type/message and the
  exact sequence of cleanup actions. Add kernel boundary tests for new APIs.
- Keep all existing broker/transport/runtime/integration tests unchanged.
- Run targeted new tests, then `bin/lint`, container and Codex discovery.
- Follow `.github/workflows/ci.yml` for all three broker socket suites, including
  the ResourceWarning gate, and real Podman integration (14 tests, no skips).
- Actual host smoke remains 6-6; local Podman is unavailable in this session.

## PR #95 CI investigation

- Run `33939070584`, attempt 1: Unit tests passed; egress Podman tests passed;
  Family failure-injection subcase `(agent='claude', with_egress=False)` failed.
- Failure path: test line 748 -> podman.register_runtime -> Family runtime line
  274 -> registration error -> podman._stop_named_container -> cleanup error.
- The saboteur waits only for `podman container exists`, so it may stop the
  runtime before registration. Container existence is not registration readiness.
- The **same traceback** occurs on pre-6-3 main `a5543b7`, run `33934145356`,
  with `(agent='codex', with_egress=True)`. The three relevant production/test
  files are byte-identical between that commit and PR #95. This establishes
  a pre-existing failure; the precise Podman stop/kill failure remains unproven
  because the helper suppresses subprocess diagnostics.
- Run `33935429792` on `421aa55` failed a different Family inspection assertion;
  do not conflate it with the registration/cleanup failure.
- Attempt 2 of `33939070584` reran only the failed job on the same head and
  passed. This is not a fix for the existing Family race.
- Merge was then refused because branch protection requires up-to-date main.
  Updated the branch through the GitHub API with expected_head_sha, producing
  `86fefc1e7dcc6c5bdd178e18f5088e3f28db297b`; new CI run `33939621566` passed
  both required jobs before merging. No admin bypass or force push was used.
- Main's two relevant Podman unit tests pass after `tempfile.gettempdir()` is
  called before loading/running them. A direct isolated run initially failed
  because the os.write mock captured stdlib tempfile's first-use `b'blat'`
  directory probe (`/usr/lib/python3.14/tempfile.py:207`). No test was changed.

## Self-review

- Source versions and private inventory are recorded separately from CI state.
- Permission strengthening, short-write fixes, lifecycle changes, and existing
  Family failures are not silently included in the GitHub refactor.
- This is a planning input document, **not an approved implementation plan**.
  The selected approach is now covered by the separate implementation plan;
  this investigation remains historical evidence, not an implementation record.
