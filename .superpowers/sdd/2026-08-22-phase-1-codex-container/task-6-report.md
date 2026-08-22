# Task 6 report: runtime launch and safe doctor

## RED/GREEN evidence

- RED: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest.test_run_validates_then_starts_codex -v` failed with `1 != 0` because `run` dispatch was not implemented.
- GREEN: the same focused command passed 1 test after runtime preflight and dispatch were added.
- RED: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest.test_doctor_reports_presence_without_secret_values -v` failed with `1 != 0` because `doctor` dispatch was not implemented.
- GREEN: the same focused doctor command passed after the ordered check pipeline was added.
- RED: the focused configured-state-root and metadata-handover-root symlink tests each failed with `0 != 1`, proving both paths reached the runner before canonical-path checks.
- GREEN: both symlink tests passed after exact configured-root validation and Task 6-local metadata path validation were added.
- RED: the ancestor-symlink state-root test failed with `0 != 1`; the malformed metadata test errored with an uncaught `TypeError`.
- GREEN: both passed after checking every configured root component and validating metadata field types before `ProjectRecord.read()`.
- Final focused: the prescribed run test passed 1 test.
- Final container: `PYTHONPATH=src python3 -m unittest discover -s tests/container -v` passed 45 tests.
- Final full: `PYTHONPATH=src python3 -m unittest discover -s tests -v` passed 63 tests.
- `git diff --check` exited 0.

## Files

- Modified `src/agent_container/agentctl.py`.
- Modified `src/agent_container/podman.py`.
- Modified `tests/container/test_agentctl.py`.
- Modified `tests/container/test_podman.py`.
- Added this report.

## Self-review

- `run` validates canonical/private managed state, mode-0600 metadata and credentials, real workspace/`.git`, exact HTTPS origin, canonical metadata handover root, and a contained non-symlink project directory before constructing `run_codex_spec`; it then invokes the runner exactly once.
- Refusal coverage independently proves state/private-directory/file mode failures, malformed or symlinked metadata, auth/GH/workspace/`.git`/handover symlinks, origin mismatch, and UID/GID mismatch cannot reach runtime execution.
- `ProjectRecord.read()` and the established `main(..., runner, git_remote_reader, stdout, stderr)` injection interface remain unchanged. `run_command` only gains an optional capture flag used by production doctor probes.
- Doctor emits fixed ordered `PASS|WARN|FAIL  check: detail` lines, never reads or prints credential contents, exits nonzero only when a FAIL exists, and always warns that Phase 1 outbound network is not domain-restricted.
- Normal run output contains only the project start line; it does not print the Podman argv or absolute auth path.
- No real Podman, GitHub, Git mutation, or Codex command ran. All external boundaries used injected fakes.

## Concerns

- No known implementation concerns. Real-host Podman behavior was intentionally not smoke-tested in this task, per scope.

## Fix Round 1

### Findings addressed

- Added an explicit caller/runtime-preflight identity gate. An injectable identity reader is checked against the real process UID/GID before the runtime spec builder is called; the existing `run_codex_spec` identity validation remains as defense-in-depth.
- Converted base filesystem `OSError` failures in doctor state, credential, metadata, workspace, and handover checks into sanitized fixed FAIL results. Remaining command-level filesystem `OSError` exits `1` without a traceback or exception text.

### RED/GREEN evidence

- RED: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest.test_run_rejects_identity_mismatch_before_building_runtime_spec -v` failed with `TypeError: main() got an unexpected keyword argument 'identity_reader'`, confirming no caller-side injectable identity boundary existed.
- GREEN: the focused identity mismatch plus valid-run command passed 2 tests; the mismatch test proved both the runtime spec builder and runner remained untouched.
- RED: the focused doctor/run filesystem-error command failed with 4 errors: private-state, project-metadata, handover-project, and outer run paths each propagated `OSError: DO-NOT-PRINT-CREDENTIAL-BODY`.
- GREEN: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest.test_run_rejects_identity_mismatch_before_building_runtime_spec tests.container.test_agentctl.AgentCtlRunDoctorTest.test_doctor_converts_filesystem_oserrors_to_ordered_failures tests.container.test_agentctl.AgentCtlRunDoctorTest.test_run_converts_filesystem_oserror_without_running_container -v` passed 3 tests. Each doctor subcase retained the same ten check names/order and omitted the exception sentinel.
- GREEN: `PYTHONPATH=src python3 -m unittest discover -s tests/container -v` passed 48 tests.
- GREEN: `PYTHONPATH=src python3 -m unittest discover -s tests -v` passed 66 tests.
- `git diff --check` exited 0.

### Files and self-review

- Modified `src/agent_container/agentctl.py`, `tests/container/test_agentctl.py`, and this report only.
- The main injection interface remains backward-compatible: the identity reader and runtime builder are optional trailing dependencies, and existing callers require no changes.
- UID/GID validation now completes inside runtime preflight before builder construction; the builder still independently rejects non-current identities.
- Non-permission/not-found `OSError` text is not rendered, preventing exception messages from disclosing credential-like content. Doctor continues through its fixed checks and WARN line after handled filesystem failures.
- The deferred double parse of `project.json` was not refactored.

### Concerns

- None. No real Podman, GitHub, Git mutation, or Codex operation ran.
