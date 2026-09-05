# Phase 6-5 Family broker implementation plan

> Execute tasks sequentially in this session with executing-plans. Independent
> read-only review uses requesting-code-review. Existing approvals cover normal
> compatibility-preserving implementation choices; do not repeat permission requests.

**Goal:** Share Family frame encoding/decoding and accept iteration while preserving
wire, exceptions, audit, PID validation, shutdown order and resource ownership.
**Architecture:** Reuse existing kernel FrameSchema/JsonOptions/encode_frame/decode_frame
and accept_clients. Family keeps exact-type guards, JSON callback, stream operations,
client/transport, start/close, descriptor cleanup and pending/audit transaction.
**Tech stack:** Python >=3.11, unittest, Ruff, rootless Podman CI; no new dependencies.
**Spec:** `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`, 6-5 compatibility scope.
**Baseline:** `39fbc5e997946cc915013190b78139eb09c94d48`.

## Constraints and investigation

- kernel imports no broker-specific module; no kernel API change is needed.
- Existing tests stay byte-for-byte unchanged. No protocol version, Mount, StateLayout,
  Podman, credential, network or state schema change. No unrelated bug fix.
- Family accepts before PID registration, then validate_peer rejects per connection.
  A readiness wait would change that behavior. Keep registration/validation in Family.
- Family's stream helpers reject bytes/int subclasses and sanitize read/write/flush
  exceptions. Kernel's existing helpers differ, so keep Family helpers unchanged.
- Frame maximum includes 4-byte header: request16384 / response1024. Kernel maximum
  is body-only; use maximum - 4. Keep original JSON decoder callback and exact input
  type, plus field/type/payload validation. Preserve helper names for compatibility.
- Family audit append is a locked transaction with identity checks, rollback and
  parent fsync. It cannot use current AuditLog without changes; keep it intact.
- Lifecycle captures directory/socket descriptors and inode identity, interrupts the
  active client on failure, stops after consumed request and sanitizes errors. Keep
  every lifecycle method except the shared accept/timeout iteration unchanged.
- New helper `_decode_frame(data, *, maximum, kind, fields)` rejects non-exact bytes,
  then calls kernel decode with a Family JSON callback. `_frame_body` stays callable
  as an unchanged legacy private helper, although public decode uses the new adapter.

## Task 1 — Fix baseline evidence

Files: new `tests/container/broker_family_golden_support.py`,
`tests/container/test_broker_family_golden.py`,
`tests/fixtures/broker_family_golden.json`,
`tests/container/test_family_kernel_compatibility.py`.

- [ ] Run existing Family protocol/client/runtime/transport/broker baseline suites.
- [ ] Build a fixed synthetic UTF-8 request and pending response collector, including
  request frame at the16384 boundary and audit events appended through the real
  Family audit API to a private temporary directory. Store hex bytes, not live values.
- [ ] Generate the static fixture using an archive of the baseline src plus the
  collector. Do not regenerate from the modified encoder. Add golden tests comparing
  encode output and decode values/consumed length with trailing bytes.
- [ ] Add characterization of exact bytes rejection, duplicate/non-object/invalid JSON,
  response total maximum, partial stream operations and sanitized error messages.
- [ ] Run the new tests against the old implementation; deliberately corrupt a copy
  of a golden expectation to demonstrate the comparison catches changed wire bytes.

## Task 2 — Share Family protocol primitives

Files: `src/agent_container/family_intake_protocol.py` only.

- [ ] Add imports for FrameSchema, JsonOptions, encode_frame, decode_frame.
- [ ] Construct schemas with label `family intake {kind}`, stream_label `family intake
  stream`, fields as the existing request/response field sets, max_bytes=maximum-4,
  JsonOptions(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).
- [ ] `_encode(values, maximum, kind)` delegates to encode_frame with that schema.
- [ ] Public decoders delegate to the new adapter, retaining typed validators.
  The adapter begins `if type(data) is not bytes: raise ValueError(...)` and uses
  `json_decoder=lambda body: _decode_json(body, kind)`. Preserve private helpers.
- [ ] Run golden, characterization and all existing Family suites. Differentially
  compare baseline/current public codecs on valid/invalid inputs and exceptions.

## Task 3 — Share accept iteration

Files: `src/agent_container/family_intake_runtime.py` and new
`tests/container/test_family_kernel_runtime.py`.

- [ ] Characterize timeout, pre-stopped loop, stop during accept, accept failure,
  consumed/failed session and handler exception using ordered fake listener/client
  fixtures. Assert actual client close/timeout, stop/error state and handler outcome.
- [ ] Replace only the outer loop with
  `for client in accept_clients(listener, stop_event=self._stop):`.
  Retain the client lock and stop check before assigning `_client`, inner context
  manager/handler/finally and failed/consumed actions verbatim.
- [ ] Run new and existing runtime/transport suites plus socket integration.

## Task 4 — Verify and integrate

Files: spec above, this plan, roadmap and CHANGELOG.

- [ ] Verify no baseline tests or excluded source files changed; compare retained
  protocol helpers and runtime methods via AST; independently regenerate golden
  from the baseline archive and check identical bytes/hash.
- [ ] Run bin/lint, container/Codex suites, all broker socket suites and forced-unknown
  tests with socket integration enabled. Record failures and exact counts honestly.
- [ ] Update 6-5 scope/evidence in spec, roadmap and CHANGELOG. Keep Phase6 in progress;
  authenticated host smoke is not run here and remains the6-6 gate.
- [ ] Independent review before pushing the final immutable feature head and creating
  the PR. Required CI Unit tests and Podman integration must pass, all14 real Podman
  tests without skips. Analyze any failure before retrying; do not assume a known race.
- [ ] Merge the verified head, confirm merged tree equality and local main sync.
  Preserve existing unrelated worktrees and the11 empty untracked root files.
