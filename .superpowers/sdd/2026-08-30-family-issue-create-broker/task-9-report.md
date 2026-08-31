# Task 9 report

## Changes

- Extended both Codex and Claude runtime specifications with an optional, exact
  `FamilyRuntimeMount`: one read/write socket-directory bind and exactly the two
  family environment entries.
- Bound the host source path to the selected project's derived intake run root
  and a lowercase 16-hex run ID. Forged, relative, cross-project, or inconsistent
  mount values fail before Podman is invoked.
- Added optional-binding discovery inside the existing `ExitStack`. A valid
  binding starts one `FamilyIntakeRuntime`; unbound projects retain the previous
  launch contract. Startup, builder, registration, supervision, and cleanup
  failures abort without an unprotected relaunch.
- Added a stopped-child launch handoff. A fixed shell stops itself before exec,
  the parent observes `SIGSTOP`, registers that exact PID/start-time, and only
  then sends `SIGCONT`. Exec preserves the PID and Podman remains the registered
  runtime root for the Podman/conmon/container descendant check. Registration
  failure kills and reaps the stopped child without resuming it.
- Added an opt-in rootless Podman test for both agent labels. It exercises one
  accepted request, a rejected second request, private-value absence from the
  launch surface, and socket cleanup when an instrumented image is available.

## TDD evidence

- RED: the new exact mount test failed with `TypeError: run_codex_spec() got an
  unexpected keyword argument 'family_mount'`.
- RED: stopped-child registration tests failed because
  `run_command_supervised` accepted only three arguments.
- GREEN: the four new Podman contract/handoff tests pass.
- GREEN: bound `agentctl run` lifecycle/supervisor test passes.
- Focused Podman plus run/doctor: 99 tests passed.
- Full container suite outside the socket-restricted sandbox: 894 tests passed,
  1 skipped.
- Integration suite outside the socket-restricted sandbox: 23 tests passed,
  13 skipped.
- `bin/lint`: passed.
- `git diff --check`: passed.

The first sandboxed full run produced five pre-existing egress socket
`PermissionError`s and five family-intake socket startup/cleanup errors. The
approved host-side equivalent passed all 894 tests.

## Podman evidence

`not run`: this host has no `podman` executable (`podman: not found`). The new
integration test reports the concrete prerequisite as
`rootless Podman executable is unavailable`. Therefore real Podman/conmon
ancestry, mounted-socket operation, stale-runtime behavior, and inspection-based
non-exposure are not claimed as observed PASS on this host. Unit and existing
real-process synthetic ancestry coverage remain PASS.

## Remaining concern

The stopped-shell-to-`exec podman` handoff is unit-proven and preserves the PID,
but the actual rootless Podman/conmon process tree still requires execution on a
host with rootless Podman and the instrumented image before the completion gate
can be marked PASS.

## Fix round 1

Addressed the five Important review findings:

- Family-only launches now receive one deterministic, non-secret Podman name.
  Family health failure, signal interruption, and CLI reap/wait failure stop that
  exact container and fall back to a bounded forced kill. Egress-enabled runs
  retain the existing egress name and cleanup path, so no second or ambiguous
  container target is introduced. Cleanup errors remain fatal.
- The opt-in Podman fixture now derives both runtime commands from the real
  `run_codex_spec` and `run_claude_spec` paths, then substitutes only the
  instrumented probe command. The probe asserts exactly two family variables,
  forbidden-value absence in environment, `/proc/self/mountinfo`, and the
  visible filesystem, one accepted request, a denied second request, and owned
  socket cleanup. Real Podman execution remains `not run` on this host.
- Family-only supervision sleeps for a bounded 0.1 seconds per health-check
  iteration; the unit test observes the wait and continued failure response.
- `FamilyRuntimeMount` moved to the credential-neutral
  `family_runtime_mount.py`. `agentctl` imports host-only runtime/state code only
  inside host command branches. Host-only intake broker/runtime/transport and
  pending modules are excluded from the effective image source set, with import
  boundary regression assertions.
- The mount captures run-directory and socket device/inode plus type, UID, and
  mode after creation. Spec generation and the final pre-`Popen` handoff walk
  every path component with descriptor-relative `O_NOFOLLOW`, then require the
  same identities. Socket and run-directory replacement race tests pass; no fd
  path is used as the Podman mount source.

RED evidence included missing family cleanup calls, missing bounded wait,
top-level host-only imports in effective image code, and acceptance of replaced
mount inodes. GREEN evidence after this round: focused agentctl/podman/image 212
PASS; family runtime 12 PASS outside the socket sandbox; full container 897
PASS; integration 23 PASS / 13 prerequisite skips; lint and diff-check PASS.

Open environmental risk: real rootless Podman/conmon ancestry and inspection
assertions are still not observed because the host has no `podman` executable.

The round-1 minor was then completed with a real
`AgentCtlFamilyRuntimeTest` class. Its four table-driven tests directly cover
bound and unbound Codex/Claude routing, startup failure, spec failure,
registration failure, health/supervision failure, ExitStack cleanup, and the
absence of an unprotected fallback launch. This exposed and fixed a missing
fixed-error funnel: `FamilyIntakeRuntimeError` now derives from the
credential-neutral `FamilyRuntimeError`, which `agentctl` handles without
importing host-only runtime code at module load. The plan's exact focused
command passes 41 tests; the final full container rerun passes 901 tests.

## Fix round 2

- Removed the accidental runtime/mount identity mismatch. Supervision now takes
  the credential-neutral `FamilyRuntimeMount` explicitly and uses its exact
  container name for family-only stop/kill; runtime health and PID registration
  remain on `FamilyIntakeRuntime`. Tests use interface-constrained autospecs.
- Replaced the raceable pathname bind source with a held run-directory
  descriptor and `/proc/self/fd/N`. `CommandSpec.pass_fds` carries that exact fd
  through the stopped shell into Podman. The pinned fd, socket identity, and
  no-follow pathname are checked at spec construction and immediately before
  `Popen`; runtime cleanup closes the duplicate. Ancestor close-error injection
  proves the newly opened child fd is not leaked.
- Hardened the opt-in Podman fixture with `set -eu`, explicit expected-failure
  control flow, a handover path outside state, live `podman inspect` assertions,
  post-one-shot no-fallback denial, stale exited-PID registration rejection,
  forced broker-health failure, and actual container disappearance checks. A
  non-skipped shell syntax test runs even when Podman is absent.
- Extended every startup/spec/register/health/supervision failure case across
  both Codex and Claude in `AgentCtlFamilyRuntimeTest`.
- Built a temporary effective image source tree with every host-only module
  absent and proved fresh isolated `python -I` imports of `agentctl` and `podman`.

Round-2 GREEN: focused 56 PASS / 1 Podman prerequisite skip; descriptor/socket
focused 23 PASS outside sandbox; full container 903 PASS; integration 24 PASS /
13 skips; lint and diff-check PASS. Rootless Podman support for the inherited
`/proc/self/fd/N` source and all live inspect/cleanup assertions remains `not
run` on this host because the Podman executable is absent; no real-host PASS is
claimed.

## Fix round 3

- Added Podman 5.8's exact image-preceding `--preserve-fd=<N>` option for the
  pinned family directory descriptor. The same single integer now appears in
  the bind source `/proc/self/fd/N`, `CommandSpec.pass_fds`, and preserve flag.
  Descriptors below 3 are rejected, the host copy remains CLOEXEC, and a fake
  Podman-to-OCI exec probe proves the Python/stopped-shell inheritance chain.
  Podman versions below 5.8 or malformed version output fail before family
  runtime startup.
- Preserving the descriptor into the container exposes only a handle to the
  same private run directory already mounted read/write. It cannot traverse to
  the parent or another host path, adds no content beyond the socket directory,
  and the agent process may close the redundant descriptor after mount setup.
  The host runtime retains and closes its own duplicate for lifecycle safety.
- A nonzero Podman exit now cleans the exact family-only or egress-owned
  container before re-raising the original `CalledProcessError`. Stop failure
  falls through to bounded kill; cleanup failure remains fatal. Tests cover
  both target choices and stop/kill failure alongside the existing signal,
  poll/wait/reap, registration, and health boundaries.
- Podman-independent fixture tests now build both real Codex and Claude specs
  and assert preserve flag/order, pass-fd, bind source, and non-overlapping
  handover shape. The live fixture replaces timing sleep with an explicit
  inspect-ready/inspect-done filesystem barrier; the second request begins only
  after the first response has durably consumed the capability and inspection
  has completed.

Round-3 GREEN: focused 49 PASS / 1 Podman skip; socket/descriptor focused 23
PASS outside sandbox; full container 908 PASS; integration 25 PASS / 13 skips;
lint and diff-check PASS. Actual Podman/conmon `--preserve-fd` operation remains
`not run` solely because this host has no Podman executable.

## Fix round 4 security ruling

The round-3 statement that a preserved run-directory descriptor could not
traverse to a parent was incorrect. A directory fd is an ambient `openat`
capability, so an agent-controlled process could use `..` components to reach
host state outside the intended socket directory. The directory-fd mount design
is withdrawn completely.

The replacement keeps the no-follow directory walk and directory descriptor
host-only for identity validation. It additionally pins the `intake.sock` inode
with `O_PATH|O_NOFOLLOW`; only this non-directory Unix-socket fd appears in
`CommandSpec.pass_fds`, Podman's exact `--preserve-fd=N`, and the single-file
bind source. The fixed destination is `/run/agent-family/intake.sock`. A
root-owned image launcher is the first process for Codex, Claude, and egress
variants; it closes exactly N before executing any agent-controlled command.
Regression probes prove the pin is a socket, cannot be used as a directory for
parent traversal or accept/listen, and is absent after launcher exec. No
preserved directory fd remains.

Family-bound run and doctor now fail closed unless the version parses as local
Podman >=5.8, the selected OCI runtime is exactly `crun`, connection JSON is
well formed with no default remote connection, and neither `CONTAINER_HOST` nor
`CONTAINER_CONNECTION` selects a remote service. The live integration probe has
a bounded container-side barrier, host-side subprocess timeouts, and a
`finally` release marker so inspection failure cannot hang the container.

RED evidence: doctor emitted no family-specific prerequisite checks; the live
probe's inspect barrier was unbounded; explicit remote environment selection
reached family runtime startup; and the inherited-fd probe used a directory.
Focused GREEN is 48 PASS for podman plus `AgentCtlFamilyRuntimeTest`; socket,
image, and integration-shape GREEN is 29 PASS with the one live Podman test
reported as prerequisite-skipped.

Final round-4 verification: full container suite 911 PASS; full integration
suite 25 PASS / 13 prerequisite skips; `bin/lint` PASS; `git diff --check`
PASS. The focused family class grew to 7 tests after adding runtime and doctor
coverage for runc, configured remote service, remote environment override,
malformed connection JSON, and Podman 5.7.

Cost if this ruling is wrong: single-file binding an `O_PATH` Unix-socket fd
through Podman/crun is deliberately treated as an availability gate, not a
reason to restore the unsafe directory capability or pathname-only recheck.
This host has no Podman executable, so the actual rootless Podman/crun
file-bind, ancestry, one-shot, inspect, and forced-cleanup path remains **not
run**. Deployment must keep the feature fail-closed until that exact host gate
is observed successfully.
