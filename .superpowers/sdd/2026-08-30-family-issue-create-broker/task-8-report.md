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
