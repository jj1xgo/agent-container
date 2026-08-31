# Task 8 report: host-approved family Issue CLI

## Outcome

Implemented the exact `agentctl family ...` command tree for binding, inventory,
doctor, pending metadata, canonical preview, interactive approval, rejection,
and both explicit reconciliation paths.

The irreversible approval controller now:

- requires a TTY and exact `approve <request-id>` confirmation;
- compares the previewed request and binding again under one continuously held
  `pending_lock`;
- revalidates the exact installation inventory/permission boundary before send;
- durably moves `pending -> sending`, makes exactly one creator call, and then
  durably moves to `created`, `pending`, or `unknown` according to the proven
  send boundary;
- emits success output/audit only after the terminal state and required content
  cleanup are durable;
- retains `unknown` without retry for post-send or unclassified uncertainty.

`resolve-created` requires an operator-supplied issue number and exact
`verify_existing` result. `resolve-not-created` requires a TTY and exact
`not-created <request-id>` confirmation, warns that a later approval can create
external state, and performs only `unknown -> pending`.

The focused orchestration is in the host-only `family_cli.py` module, which is
explicitly excluded from the container build context. Tests inject complete
fake provider, creator, and inventory boundaries; no Task 8 test performs real
network I/O.

## Files

- `.containerignore`
- `src/agent_container/agentctl.py`
- `src/agent_container/family_cli.py`
- `src/agent_container/family_state.py`
- `tests/container/test_agentctl.py`
- `tests/container/test_image.py`

`family_state.write_family_binding` gained a default-compatible
`replace_existing` keyword so a fresh bind can reject a concurrent binding
winner while holding the existing cooperative binding lock.

## TDD evidence

### Initial command-tree and read-path RED

Command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlFamilyTest -v
```

Result: exit `1`; six initial tests produced 15 errors. The parser reported
`family` as an invalid command, and injected family dependency keywords were
absent from `main`.

The host-only image-boundary test was separately run before adding the module:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_image.ContainerImageContractTest.test_containerignore_is_an_allowlist_for_build_inputs_only -v
```

Result: exit `1`, one failure because the required
`src/agent_container/family_cli.py` deny entry was absent.

After minimal parser/bind/list/doctor/pending/preview implementation, the six
CLI tests and two image-boundary tests passed (`Ran 8 tests ... OK`).

### Approval, rejection, race, and audit-order RED

Command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlFamilyTest -v
```

Result after adding the irreversible-boundary tests: exit `1`, 16 tests run,
10 failures. Approval/reject dispatch had no implementation: no creator call,
no expiry/concurrent terminal transition, no `unknown`, and no durable success
path.

After the minimal locked controller implementation, the same 16 tests passed.
The success-audit injection observed a `created` record with neither `title`
nor `body` and no displaced content inode before the success event was
appended. The cleanup-failure injection proved no success event or issue-number
output occurs after cleanup ambiguity.

### Reconciliation RED

The reconciliation and explicit helper tests were added before implementation.
The focused command then exited `1`: 23 tests ran with six failures and one
error. `_approve_family_issue` was absent, neither reconciliation command
dispatched, and `verify_existing` was never called.

After implementation, all 23 tests passed. `resolve-created` exercised only
`verify_existing` with the supplied issue number; `resolve-not-created`
exercised exact TTY confirmation and preserved canonical content while moving
only to `pending`.

### Adversarial review RED/GREEN cycles

Each review finding received a failing regression before its fix:

- Live inventory rejected a realistic full GitHub repository object:
  `ValueError: family repository inventory failed`. After allowing documented
  extra repository fields while requiring exact `full_name` and numeric `id`,
  the test passed.
- Duplicate inventory `id` keys were silently last-wins; the regression failed
  with `ValueError not raised`. Strict duplicate-key decoding made it pass.
- A binding created during inventory resolution was overwritten; the
  regression expected exit `1` but got `0`. The lock-held exclusive-create mode
  preserved the concurrent winner.
- Preview produced no fixed event; the regression failed with an empty audit
  inventory. Preview now appends its content-free event before printing.
- A 5,000-digit issue number produced a 5,237-character argparse diagnostic;
  the bounded-output assertion failed. Length is now rejected before integer
  conversion.
- A creator result naming another repository was accepted as success; the
  regression expected exit `1` but got `0`. Such an unproven result now becomes
  `unknown` with no retry or success event.
- Fresh bind could not immediately list an empty inventory; the regression got
  exit `1`. Bind now provisions the private pending and audit directories.
- Non-TTY and post-preview race denials initially had no audit event; the tests
  failed on empty audit inventory. They now emit only fixed `denied` metadata.

## Final verification

Focused Task 8 command:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest -v \
  && bin/lint && git diff --check
```

Result:

```text
Ran 27 tests in 0.193s
OK
All checks passed!
```

Full existing agentctl regression:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl
```

Result:

```text
Ran 150 tests in 0.895s
OK
```

The restricted sandbox family run exposed the existing real-socket environment
constraint: the first swallowed failure was `PermissionError [Errno 1]` at the
descriptor-relative socket `chmod`. Rerunning the exact family discovery with
the required unrestricted socket permissions produced:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_family*.py'
Ran 188 tests in 1.803s
OK
```

Final complete-suite command (outside the restricted socket sandbox):

```text
PYTHONPATH=src python3 -m unittest discover -s tests
```

Result:

```text
Ran 912 tests in 4.742s
OK (skipped=12)
```

No real GitHub operation was performed.

## Review fix round 1

The first review fix round hardened the host boundary in four connected areas:

- every pending lock now requires the expected project ID, and the opaque
  pre-prompt `PendingSnapshot` binds the decoded request to its exact validated
  bytes and file device/inode; approval reacquisition rejects even an
  equal-bytes replacement inode before any creator call;
- approval receives a clock callable and samples it under the request lock,
  after confirmation and provider/inventory preflight, immediately before the
  `pending -> sending` transition; equality with `expires_at` durably expires
  and cleans the request without sending;
- every post-send `CreatedIssue` is validated as an exact class with an exact
  non-boolean positive bounded integer and exact string URL before the created
  transition; malformed results durably become `unknown` with a response-stage
  audit;
- family command startup recovers surviving `sending` records to `unknown` and
  emits only the fixed `recover/unknown/reconcile` event after each durable
  recovery. The initializer returns only content-free recovered request and
  project IDs. Intake-runtime recovery emits the same event.

Doctor now uses a bounded read-only pending inspector and bounded exact JSONL
audit validator. It does not create lock files, mutate requests, recover, or
append audit. A surviving `sending` record is a recovery-required failure, and
malformed, cross-project, oversized, replaced, or insecure audit files fail the
audit check. Irreversible confirmation now requires `fileno()` plus
`os.isatty(fd)` and performs one bounded line read; Task 8 tests use real PTYs
and reject `isatty()` spoofing, absent/unusable file descriptors, and overlong
confirmation.

Review-round production changes also touch:

- `src/agent_container/family_pending.py`
- `src/agent_container/family_intake_runtime.py`
- the corresponding family pending, intake broker/runtime/transport, and
  intake socket tests whose layout-aware API calls now supply the exact project
  ID.

### Review round 1 RED

The complete new regression selection was first run against the reviewed
implementation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_pending.PendingStoreTest.test_pending_lock_requires_and_validates_expected_project_before_yield \
  tests.container.test_family_pending.PendingStoreTest.test_pending_snapshot_rejects_equal_bytes_on_a_new_inode \
  tests.container.test_family_pending.PendingStoreTest.test_initializer_returns_only_safe_metadata_for_recovered_sends \
  tests.container.test_family_pending.FamilyAuditTest.test_read_only_audit_validator_is_bounded_exact_and_project_scoped \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_family_issue_commands_reject_cross_project_injected_records \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_approve_rejects_same_content_inode_replacement_without_sending \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_malformed_created_scalars_become_unknown_at_response_stage \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_approval_samples_expiry_after_preflight_at_exact_boundary \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_cli_startup_recovers_sending_then_allows_exact_reconciliation \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_doctor_is_read_only_and_fails_sending_or_invalid_audit \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_irreversible_confirmation_requires_real_fd_and_bounded_line \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_falsy_injected_family_dependencies_are_used_when_not_none -v
```

Result: exit `1`; 12 test methods produced 21 failures. The observed failures
were the intended ones: no project argument or snapshot/audit APIs, all four
cross-project commands succeeded, equal-bytes inode replacement sent, all five
malformed issue numbers remained `sending`, the boundary-expired request sent,
startup reconciliation rejected surviving `sending`, doctor passed unsafe
state, an `isatty()` spoof authorized a send, and falsy injected dependencies
were discarded.

After the first green pass, the stronger doctor mutation assertion was run
before its fix:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_doctor_is_read_only_and_fails_sending_or_invalid_audit -v
```

Result: exit `1`; one test method produced two subtest failures because doctor
created `.REQUEST.json.lock` for malformed and insecure audit cases. The
read-only pending inspector removed that write.

### Review round 1 GREEN and final verification

The original 12-method regression selection passed after the minimal fixes:

```text
Ran 12 tests in 0.080s
OK
```

Final focused Task 8 and pending-store commands:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest -v
Ran 35 tests in 0.280s
OK

PYTHONPATH=src python3 -m unittest tests.container.test_family_pending
Ran 36 tests in 0.116s
OK
```

Full agentctl regression:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v
Ran 158 tests in 0.986s
OK
```

Full family regression outside the restricted real-socket sandbox:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_family*.py'
Ran 192 tests in 1.820s
OK
```

Final complete verification outside the restricted real-socket sandbox:

```text
PYTHONPATH=src python3 -m unittest discover -s tests && bin/lint && git diff --check
Ran 924 tests in 4.837s
OK (skipped=12)
All checks passed!
```

No real GitHub operation was performed in review round 1.

## Review fix round 2

The second review fix round closes the remaining post-send, startup-recovery,
and terminal-source ambiguity:

- approval now catches every ordinary exception raised while validating the
  creator's returned object, including missing attributes and hostile
  properties. The catch surrounds result validation only; the subsequent
  durable `sending -> unknown` transition and fixed
  `approve/unknown/response` audit are not swallowed, and `BaseException`
  remains outside the ordinary failure path;
- interrupted-send recovery now persists an exact internal
  `recovery_audit_pending: true` marker with `sending -> unknown`. While still
  holding the same request lock, startup appends the fixed
  `recover/unknown/reconcile` event and only then durably clears the marker.
  Invalid clock or audit prerequisites fail before mutation. Audit open,
  write, fsync, or process-crash failures leave the marker for retry by the
  next CLI or intake-runtime startup, and reconciliation remains blocked on
  that request lock until append and clear finish;
- recovery is deliberately at-least-once. A crash after the durable audit
  append but before marker clear can duplicate the fixed recovery event on
  restart, but cannot omit it. Doctor remains read-only and reports either a
  surviving `sending` record or an `unknown` record with the internal marker
  as recovery-required;
- irreversible confirmation duplicates the supplied `fileno()` immediately,
  verifies the duplicated descriptor with `os.isatty`, and reads the bounded,
  newline-terminated UTF-8 confirmation directly with `os.read`. It rejects
  EOF, invalid UTF-8, overlong input, and extra input. It never consults
  `stdin.readline`, and closes the duplicate in `finally`; closing and reusing
  the original fd cannot redirect the confirmation source.

The recovery marker is excluded from representations and all CLI/container
output. The exact pending JSON decoder accepts it only as boolean `true` on an
`unknown` content-bearing record; normal and terminal schemas remain exact.
Point recovery was also tightened so requesting an ineligible record cannot
recover an unrelated interrupted sibling.

### Review round 2 RED

Malformed returned-object regression before broadening the result-validation
catch:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_malformed_created_objects_never_escape_response_quarantine -v
```

Result: exit `1`; one method with three failing subtests. Forged
`object.__new__`, deleted-attribute, and hostile-property results all left the
request in `sending` instead of `unknown`.

Recovery prerequisite regression before the coupled API existed:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_pending.PendingStoreTest.test_recovery_validates_clock_before_mutating_sending -v
```

Result: exit `1`; `TypeError` reported that `initialize_pending_store` did not
accept `audit_path`, proving recovery could not yet couple durable state and
audit.

Terminal source-binding regression before direct duplicated-fd reads:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_irreversible_confirmation_requires_real_fd_and_bounded_line -v
```

Result: exit `1`; a wrapper exposing a real PTY fd while fabricating the exact
`readline()` value returned success and performed the creator call.

The point-recovery isolation regression was also demonstrated before its
minimal refactor:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_pending.PendingStoreTest.test_point_recovery_never_recovers_an_unrequested_sibling -v
```

Result: exit `1`; the unrequested sibling changed from `sending` to `unknown`.

### Review round 2 GREEN and final verification

Focused recovery tests cover invalid exact clocks, audit open/write/fsync
failures and repair, simulated crash repair, held-lock ordering against
reconciliation, and exact point recovery. Focused terminal tests cover the
forged stream method, invalid UTF-8, extra input, overlong input, and original
fd close/reuse while the duplicate stays pinned. The complete Task 8 class
then passed:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest -v
Ran 37 tests in 0.317s
OK
```

Full agentctl regression:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v
Ran 160 tests in 1.034s
OK
```

Final all-family regression, including the real Unix socket integration suite,
outside the restricted socket sandbox:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_issue \
  tests.container.test_family_state \
  tests.container.test_family_pending \
  tests.container.test_family_issue_create \
  tests.container.test_family_github_app \
  tests.container.test_family_intake_protocol \
  tests.container.test_family_intake_transport \
  tests.container.test_family_intake_broker \
  tests.container.test_family_intake_client \
  tests.container.test_family_intake_runtime \
  tests.integration.test_family_intake_socket
Ran 196 tests in 1.871s
OK
```

Final complete verification outside the restricted socket sandbox:

```text
PYTHONPATH=src python3 -m unittest discover -s tests
Ran 930 tests in 4.957s
OK (skipped=12)

bin/lint && git diff --check
All checks passed!
```

All creator, provider, and inventory dependencies were fakes in these tests.
No real GitHub operation was performed in review round 2.

## Review fix round 3

The third review fix round removes the last use-after-validation dependency on
an untrusted creator result. `_validate_created_result` now:

1. requires the exact `CreatedIssue` class;
2. reads `number` and `url` from that object exactly once each into locals;
3. validates only those primitive locals; and
4. returns a new private immutable `_ValidatedCreatedIssue` snapshot.

Approval and exact-GET reconciliation use only that stable snapshot for the
durable transition and output. They never read the creator-returned object
again. Ordinary exceptions raised during either attribute acquisition remain
inside the response-validation quarantine, causing durable
`sending -> unknown` plus `approve/unknown/response`. `BaseException` is still
not caught by the ordinary path, preserving the previously ruled crash
semantics and startup recovery boundary.

### Review round 3 RED

The hostile-property regression supplies an exact `CreatedIssue` whose selected
property returns a valid value twice and raises on the third access:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest.test_created_result_attributes_are_snapshotted_exactly_once -v
```

Result before the fix: exit `1`; one method with two failing subtests
(`number` and `url`). Both returned command failure instead of success because
the validated original object was read again outside the response quarantine.

After the one-read snapshot change, the same test observes exactly one access
to each hostile property and a durable created result. A companion test proves
that an ordinary exception during acquisition becomes response-stage unknown,
while `KeyboardInterrupt` remains outside the ordinary catch.

### Canonical TTY incomplete-line ruling

The proposed `approve <id>\nx` case was reproduced against the real PTY. The
first exact newline-terminated confirmation line is readable, while the `x` is
the beginning of a second, not-yet-terminated canonical input line.

A Linux PTY characterization produced:

```text
write(master, b"approve\nx")
FIONREAD before first read: 8
read(slave): b"approve\n"
FIONREAD after first read: 0
disable ICANON
FIONREAD after disabling ICANON: 1
```

`select` has the same canonical readiness boundary as `FIONREAD`: the pending
unterminated `x` is intentionally invisible until a line delimiter arrives.
There is no immediate, portable, non-destructive POSIX inspection for that
incomplete canonical line. A duplicated fd does not isolate terminal settings;
termios belongs to the terminal device, so disabling `ICANON` through the dup
also changes the original terminal and races concurrent readers and arriving
input. It can also leave or consume user input across failure boundaries.

Ruling: the helper strictly validates one bounded, newline-terminated
confirmation line. It rejects bytes after that newline when already delivered,
including a completed extra `x\n` line, but an incomplete future line is not
part of the confirmation bytes. No shared-termios mutation or timing heuristic
was added. Existing tests retain exact normal-line, completed-extra-line, EOF,
invalid UTF-8, overlong input, spoofed stream, and duplicated-fd pinning
coverage.

Cost if this ruling is wrong: meeting an “unterminated future bytes must reject
immediately” contract would require a different input protocol, such as
noncanonical input established before prompting with an explicit quiet-period
or framing rule. That would change terminal behavior for the shared device,
introduce timing semantics, and require a wider concurrency and terminal-
restoration design; it cannot be safely implemented as a local post-line
peek.

### Review round 3 GREEN and final verification

Focused hostile-result and TTY regression selection:

```text
Ran 8 tests in 0.091s
OK
```

Complete Task 8 class:

```text
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlFamilyTest -v
Ran 39 tests in 0.339s
OK
```

Full agentctl regression:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl
Ran 162 tests in 1.042s
OK
```

All-family regression, including real Unix socket integration tests outside
the restricted socket sandbox:

```text
Ran 196 tests in 1.871s
OK
```

Final complete verification outside the restricted socket sandbox:

```text
PYTHONPATH=src python3 -m unittest discover -s tests
Ran 932 tests in 4.973s
OK (skipped=12)
```

All creator, provider, and inventory dependencies were fakes. No real GitHub
operation was performed in review round 3.
