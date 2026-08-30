# Runtime Egress Domain Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, fail-closed HTTPS domain allowlist for normal Codex and Claude runtimes by removing direct container networking and tunnelling approved TLS connections through a project-scoped host gateway.

**Architecture:** A private project policy combines agent-specific managed domains with operator-added exact domains. An opted-in Podman runtime uses `--network=none`; a container loopback CONNECT adapter authenticates over a mounted Unix socket to a host gateway that validates policy, DNS, peer addresses, limits, and audit metadata while relaying opaque TLS bytes.

**Tech Stack:** Python 3.11+ standard library, rootless Podman 5.8+, Unix domain sockets, HTTP CONNECT, `unittest`, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-egress-domain-allowlist-design.md`

## Global Constraints

- The first release is opt-in for existing and new projects; default-on is out of scope.
- Restrict only normal `agentctl run` Codex and Claude runtimes. Build, auth, setup, update, registration, clone, and host GitHub broker traffic remain unchanged.
- Restricted runtimes use `podman run --network=none`; no error path may relaunch with ordinary networking.
- Permit only exact approved lowercase ASCII DNS names on TCP port 443. Reject wildcard, IP literal, local/reserved suffix, Unicode, and `xn--` input.
- Keep TLS end-to-end. Gateway and audit must never contain TLS plaintext, HTTP headers/bodies, credentials, capabilities, domains, or IP addresses.
- Project policy is a current-user-owned mode `0600` non-symlink file under the existing mode `0700` project directory.
- Runtime directory and cleanup follow existing GitHub/handover broker fail-closed patterns.
- Every behavior change is test-first. Do not claim Unix-socket, Podman, or real-host PASS without evidence from an environment that can run it.

---

### Task 1: Egress policy model and atomic persistence

**Files:**
- Modify: `src/agent_container/state.py`
- Create: `src/agent_container/egress_policy.py`
- Create: `tests/container/test_egress_policy.py`
- Modify: `tests/container/test_state.py`

**Interfaces:**
- Produces: `StateLayout.egress_policy_file`, `StateLayout.egress_broker_run_root`, and `StateLayout.egress_broker_audit_file`.
- Produces: `EgressPolicy(version: int, mode: str, additional_domains: tuple[str, ...])`.
- Produces: `validate_domain(value: object) -> str`, `load_egress_policy(path: Path) -> EgressPolicy`, `enable_egress_policy(path: Path) -> EgressPolicy`, `add_egress_domain(path: Path, domain: str) -> EgressPolicy`, `remove_egress_domain(path: Path, domain: str) -> EgressPolicy`, and `disable_egress_policy(path: Path) -> None`.
- Consumes: existing `ensure_private_directory`, `ensure_private_file`, and `validate_project_id` contracts.

- [ ] **Step 1: Write failing state-layout and policy validation tests**

Add assertions equivalent to:

```python
layout = StateLayout(Path("/state"), "demo")
self.assertEqual(layout.egress_policy_file, Path("/state/projects/demo/egress.json"))
self.assertEqual(
    layout.egress_broker_run_root,
    Path("/state/egress-broker/r") / egress_broker_project_label("demo"),
)
self.assertEqual(
    validate_domain("files.pythonhosted.org"),
    "files.pythonhosted.org",
)
for denied in (
    "EXAMPLE.com", "example.com.", "*.example.com", "127.0.0.1",
    "[::1]", "localhost", "service.local", "example.com:443",
    "https://example.com", "xn--e1afmkfd.xn--p1ai", "éxample.com",
):
    with self.subTest(denied=denied):
        with self.assertRaises(ValueError):
            validate_domain(denied)
```

Test empty labels, 64-byte labels, names over 253 bytes, control characters,
booleans/non-strings, duplicates, unsorted persisted lists, missing/extra JSON
fields, wrong version/mode, malformed JSON/UTF-8, symlinks, non-regular files,
wrong owner/mode, and unsafe parent directories.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_state \
  tests.container.test_egress_policy -v
```

Expected: FAIL because the layout properties and `egress_policy` module do not
exist.

- [ ] **Step 3: Implement domain validation and exact schema loading**

Implement the public model and validator with constants rather than a loose
URL parser:

```python
@dataclass(frozen=True)
class EgressPolicy:
    version: int
    mode: str
    additional_domains: tuple[str, ...]


def validate_domain(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 253:
        raise ValueError("egress domain is invalid")
    if value != value.lower() or not value.isascii() or value.endswith("."):
        raise ValueError("egress domain is invalid")
    labels = value.split(".")
    if len(labels) < 2 or any(_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("egress domain is invalid")
    if any(label.startswith("xn--") for label in labels):
        raise ValueError("egress domain is invalid")
    if value.endswith(_DENIED_SUFFIXES) or _ip_literal(value):
        raise ValueError("egress domain is invalid")
    return value
```

Use a duplicate-rejecting JSON object hook. Require the exact keys
`version`, `mode`, and `additional_domains`, version integer `1`, mode exact
`allowlist`, a list of at most 128 domains, and persisted sort order.

- [ ] **Step 4: Write failing atomic state-transition tests**

Cover enable-exclusive-create, add, remove, disable, duplicate/managed/absent
rejection, and injected failures for write, file `fsync`, `os.replace`, and
parent `fsync`. Each injected failure must assert that old bytes remain and no
unknown sibling is removed.

```python
policy = enable_egress_policy(path)
self.assertEqual(policy, EgressPolicy(1, "allowlist", ()))
self.assertEqual(add_egress_domain(path, "pypi.org").additional_domains,
                 ("pypi.org",))
with self.assertRaises(ValueError):
    add_egress_domain(path, "pypi.org")
self.assertEqual(remove_egress_domain(path, "pypi.org").additional_domains, ())
disable_egress_policy(path)
self.assertFalse(path.exists())
```

- [ ] **Step 5: Implement atomic transitions**

Use same-directory `os.open(..., O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`, validate
the opened inode, write all bytes, `os.fsync(file_fd)`, `os.replace`, then
`os.fsync(parent_fd)`. Revalidate the existing target immediately before
replace/delete. Generate bounded random temporary names and only unlink the
exact temp created by the current call.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_state \
  tests.container.test_egress_policy -v
bin/lint
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/agent_container/state.py src/agent_container/egress_policy.py \
  tests/container/test_state.py tests/container/test_egress_policy.py
git commit -m "feat: add private runtime egress policies"
```

---

### Task 2: Configuration CLI and local doctor contract

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: all Task 1 policy functions.
- Produces CLI: `agentctl project configure-egress PROJECT` with exactly one of `--enable`, `--add-domain DOMAIN`, `--remove-domain DOMAIN`, `--disable`.
- Produces doctor states: enabled valid policy is locally `PASS`; absent policy remains `WARN`; present invalid policy is `FAIL` and runtime preflight rejects it.

- [ ] **Step 1: Write failing parser and command transition tests**

Extend `AgentCtlParserTest` and add a focused configuration test class. Assert
mutually exclusive required actions, early project/domain validation, no
Podman runner calls, unchanged bytes after failure, and fixed output that omits
domains and state paths.

```python
result = main(
    ["project", "configure-egress", "demo", "--enable"],
    environment={"AGENT_CONTAINER_HOME": str(root)},
    runner=lambda spec: self.fail(f"unexpected runner: {spec}"),
    stdout=output,
)
self.assertEqual(result, 0)
self.assertEqual(load_egress_policy(layout.egress_policy_file).additional_domains, ())
self.assertNotIn(str(root), output.getvalue())
```

- [ ] **Step 2: Run parser/configuration tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_agentctl.AgentCtlParserTest \
  tests.container.test_agentctl.AgentCtlEgressConfigurationTest -v
```

Expected: FAIL because the subcommand is absent.

- [ ] **Step 3: Add the parser and command dispatch**

Use an argparse required mutually exclusive group:

```python
configure = project_subcommands.add_parser("configure-egress")
configure.add_argument("project")
actions = configure.add_mutually_exclusive_group(required=True)
actions.add_argument("--enable", action="store_true")
actions.add_argument("--add-domain")
actions.add_argument("--remove-domain")
actions.add_argument("--disable", action="store_true")
```

Before any write, call `_ensure_exact_state_root`, validate the registered
project directory and `project.json`, and validate the requested domain. Print
only fixed success text naming the project; `--disable` prints a warning that
the next runtime has unrestricted outbound networking.

- [ ] **Step 4: Write failing local doctor/preflight tests**

Add cases for absent, valid, malformed, unsafe-mode, symlinked, and unsupported
policy. At this task boundary doctor proves the private local policy only;
Task 5 extends the same check with the image-local adapter self-check once that
managed executable exists.

```python
self.assertIn(
    "PASS  network-policy: outbound HTTPS uses the project domain allowlist",
    rendered,
)
self.assertIn(
    "WARN  network-policy: outbound network is not domain-restricted",
    legacy_rendered,
)
self.assertIn("FAIL  network-policy:", malformed_rendered)
```

- [ ] **Step 5: Implement local doctor classification and runtime preflight loading**

Add one helper returning `EgressPolicy | None`; absence means legacy warning,
but any existing invalid path raises a fixed failure. Pass the loaded policy
forward from `_runtime_preflight` rather than reading it again after broker
startup, preventing a policy swap race. Render enabled policy as locally valid;
Task 5 will make final PASS conditional on the adapter self-check.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v
bin/lint
```

Expected: PASS for the local policy contract.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: configure and diagnose egress allowlists"
```

---

### Task 3: Versioned gateway protocol, authorization, and audit session

**Files:**
- Create: `src/agent_container/egress_broker_protocol.py`
- Create: `src/agent_container/egress_broker.py`
- Create: `tests/container/test_egress_broker_protocol.py`
- Create: `tests/container/test_egress_broker.py`

**Interfaces:**
- Produces: `EgressRequest(version, capability, project_id, sequence, operation, domain, port)` and `EgressResponse(version, status, code)`.
- Produces frame read/write helpers with a maximum metadata frame of 16 KiB.
- Produces: `EgressBrokerSession.create(layout, agent, policy)`, `authorize(request, peer_uid) -> str`, `open_listener()`, `audit(...)`, `deactivate()`, and `close()`.
- Consumes: Task 1 validated policy and agent identity.

- [ ] **Step 1: Write failing protocol tests**

Test exact request/response fields, duplicate JSON keys, booleans in integer
fields, non-canonical domain/port, wrong version/operation, invalid UTF-8,
NaN/constants, unknown fields, truncated/zero/oversized frames, trailing bytes,
and short stream reads.

```python
request = EgressRequest(1, "capability", "demo", 1, "connect", "api.example.com", 443)
encoded = encode_request_frame(request)
self.assertEqual(decode_request_frame(encoded), (request, len(encoded)))
```

- [ ] **Step 2: Run protocol tests and verify failure**

Run `PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker_protocol -v`.

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement bounded canonical frames**

Mirror the strict duplicate-rejecting approach in
`github_broker_protocol.py`, but use the exact egress fields. Accept response
statuses only `ok`, `denied`, `error`; accept fixed codes only
`authentication`, `policy`, `resolve`, `connect`, `limit`, `relay`,
`unavailable`. Encode deterministic compact ASCII JSON and require positive
sequence values no greater than `2**63 - 1`.

- [ ] **Step 4: Write failing session, authorization, audit, and cleanup tests**

Assert project/agent-scoped path length, mode/owner/non-symlink contracts,
peer UID, capability, exact monotonically increasing sequence, project,
operation, port, managed-plus-additional domain decisions, replay denial,
closed-session denial, listener double-open denial, fixed audit fields, and
exact cleanup. Include marker strings in domain, capability, IP, and exception
inputs and prove none reach audit.

- [ ] **Step 5: Implement the session**

Reuse established private runtime primitives without importing GitHub policy.
The session stores the union as a private `frozenset[str]`, keeps capability
`repr=False`, and increments the expected sequence only after successful
authorization. Audit records contain exactly timestamp, run label, project,
agent, operation, status, optional fixed stage, and optional bounded integer
byte counts. Do not store the approved domain in the record.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_egress_broker_protocol \
  tests.container.test_egress_broker -v
bin/lint
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/agent_container/egress_broker_protocol.py \
  src/agent_container/egress_broker.py \
  tests/container/test_egress_broker_protocol.py \
  tests/container/test_egress_broker.py
git commit -m "feat: add egress gateway session protocol"
```

---

### Task 4: Safe resolver, upstream connector, bounded relay, and runtime server

**Files:**
- Create: `src/agent_container/egress_gateway.py`
- Create: `src/agent_container/egress_broker_runtime.py`
- Create: `tests/container/test_egress_gateway.py`
- Create: `tests/container/test_egress_broker_runtime.py`

**Interfaces:**
- Produces: `ResolvedTarget(family: int, socktype: int, protocol: int, sockaddr: tuple)`.
- Produces: `resolve_target(domain: str, resolver=socket.getaddrinfo) -> tuple[ResolvedTarget, ...]`.
- Produces: `connect_target(target: ResolvedTarget, socket_factory=socket.socket) -> socket.socket` with one attempt and peer equality.
- Produces: `relay_tunnel(client, upstream, limits, clock, selector_factory) -> RelayCounts`.
- Produces: `EgressBrokerRuntime.create(layout, agent, policy)` context manager returning `EgressRuntimeMount`.

- [ ] **Step 1: Write failing resolver and connector tests**

Use injected resolver/socket factories; never contact the network. Cover safe
IPv4/IPv6, duplicate safe answers, mixed safe/unsafe denial, every non-global
category, malformed resolver tuples, empty answers, timeout, one-attempt-only,
and connected-peer mismatch.

```python
answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
targets = resolve_target("example.com", resolver=lambda *args: answers)
self.assertEqual(len(targets), 1)
```

- [ ] **Step 2: Run gateway tests and verify failure**

Run `PYTHONPATH=src python3 -m unittest tests.container.test_egress_gateway -v`.

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement strict resolution and single target connect**

Call `getaddrinfo(domain, 443, AF_UNSPEC, SOCK_STREAM, IPPROTO_TCP)`. Parse
each address with `ipaddress.ip_address`; require `is_global` and explicitly
reject loopback, private, link-local, multicast, unspecified, reserved, and
CGNAT `100.64.0.0/10`. If any answer is unsafe or malformed, reject the whole
set. Connect only the first de-duplicated resolver result with a 15-second
timeout and require `getpeername()` address and port to match.

- [ ] **Step 4: Write failing relay and runtime supervision tests**

Use `socket.socketpair()` and fake monotonic clocks to cover bidirectional
bytes, half-close, 64 KiB chunks, 2 GiB per-direction counters without
allocating 2 GiB, 300-second idle timeout, 7,200-second lifetime, upstream and
client errors, maximum 32 active tunnels, 128 successful creations, listener
failure, worker failure, graceful known per-connection errors, startup
failure, and exact cleanup.

- [ ] **Step 5: Implement relay and runtime**

Use `selectors.DefaultSelector`, nonblocking sockets, bounded buffers no larger
than one 64 KiB read chunk per direction, and explicit half-close state. The
runtime obtains `SO_PEERCRED`, reads one metadata frame, authorizes, reserves
limits, resolves/connects once, sends `ok`, then switches to opaque relay. It
audits fixed stages only and treats listener/supervisor failures as fatal.

- [ ] **Step 6: Run gateway/runtime tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_egress_gateway \
  tests.container.test_egress_broker_runtime -v
bin/lint
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/agent_container/egress_gateway.py \
  src/agent_container/egress_broker_runtime.py \
  tests/container/test_egress_gateway.py \
  tests/container/test_egress_broker_runtime.py
git commit -m "feat: relay bounded allowlisted egress tunnels"
```

---

### Task 5: Container CONNECT adapter and managed image entry point

**Files:**
- Create: `src/agent_container/egress_adapter.py`
- Create: `src/agent_container/egress_runtime.py`
- Create: `container/bin/agent-egress-adapter`
- Create: `container/bin/agent-egress-runtime`
- Modify: `Containerfile`
- Create: `tests/container/test_egress_adapter.py`
- Create: `tests/container/test_egress_runtime.py`
- Modify: `tests/container/test_image.py`
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces adapter CLI with fixed environment inputs `AGENT_EGRESS_SOCKET`, `AGENT_EGRESS_CAPABILITY`, `AGENT_PROJECT_ID`, and `AGENT_EGRESS_AGENT`.
- Produces wrapper: `agent-egress-runtime -- codex ...` or `-- python3 -m agent_container.claude_launcher ...`.
- Produces `egress_adapter_status_spec(image: str) -> CommandSpec` for doctor.

- [ ] **Step 1: Write failing CONNECT parser and adapter tests**

Test one canonical `CONNECT domain:443 HTTP/1.1` plus exact matching Host
header. Reject non-CONNECT, absolute URL, IP literal, alternate/missing port,
userinfo, duplicate/mismatched Host, body, transfer encoding, pipelining,
invalid line endings, controls, non-ASCII, and headers over 16 KiB. Assert
fixed `200 Connection Established` or fixed `502 Bad Gateway` without echoing
markers.

- [ ] **Step 2: Run adapter tests and verify failure**

Run `PYTHONPATH=src python3 -m unittest tests.container.test_egress_adapter -v`.

Expected: FAIL because the adapter module is absent.

- [ ] **Step 3: Implement adapter request and Unix tunnel**

Read exactly one bounded header block. Load and validate the mode `0400` or
`0444` read-only mounted capability without printing it. For each connection,
allocate a positive sequence under a lock, send an `EgressRequest`, require an
`ok` response, then use the same bounded relay primitive without inspecting
tunnel bytes. Bind loopback only; never use `0.0.0.0` or `[::]`.

- [ ] **Step 4: Write failing wrapper and image tests**

Test `--self-check`, exact managed executable paths, readiness-before-exec,
adapter failure terminating the child, signal forwarding, child exit code,
no shell invocation, no caller override of fixed gateway inputs, and
Containerfile copies with mode `0755`.

- [ ] **Step 5: Implement wrapper, scripts, image copy, and doctor probe**

The wrapper accepts only `--self-check` or `--` followed by a nonempty fixed
agent command supplied by `podman.py`. It creates a private readiness pipe,
starts the adapter, waits a bounded 5 seconds, then starts the agent in the
same process group. On either process exit it terminates the other and returns
nonzero for unexpected adapter death. Add:

```dockerfile
COPY --chmod=0755 container/bin/agent-egress-adapter /usr/local/bin/agent-egress-adapter
COPY --chmod=0755 container/bin/agent-egress-runtime /usr/local/bin/agent-egress-runtime
```

The doctor probe is a hardened, noninteractive, mount-free Podman command
running `agent-egress-runtime --self-check`. Extend Task 2's enabled doctor
case so PASS requires this probe to exit zero; missing image/adapter or nonzero
probe is a fixed `FAIL network-policy` and prevents runtime startup.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_egress_adapter \
  tests.container.test_egress_runtime \
  tests.container.test_image \
  tests.container.test_podman \
  tests.container.test_agentctl -v
bin/lint
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add Containerfile container/bin/agent-egress-adapter \
  container/bin/agent-egress-runtime src/agent_container/egress_adapter.py \
  src/agent_container/egress_runtime.py src/agent_container/podman.py \
  src/agent_container/agentctl.py \
  tests/container/test_egress_adapter.py \
  tests/container/test_egress_runtime.py tests/container/test_image.py \
  tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: add managed container egress adapter"
```

---

### Task 6: Compose the opt-in gateway into Codex and Claude runtime lifecycle

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: `EgressRuntimeMount` from Task 4 and wrapper/probe from Task 5.
- Changes: `run_codex_spec(..., egress: EgressRuntimeMount | None = None)` and `run_claude_spec(..., egress: EgressRuntimeMount | None = None)`.
- Guarantees: enabled runtime starts gateway before Podman, uses `--network=none`, fixed proxy variables and wrapper, and cleans all brokers on every exit path.

- [ ] **Step 1: Write failing Podman argv tests**

Assert enabled specs contain exactly one `--network=none`, one read-only
egress mount, fixed gateway variables, both uppercase/lowercase proxy variables,
fixed `NO_PROXY`, and `agent-egress-runtime --` before the original agent
command. Assert legacy specs are byte-for-byte unchanged and auth/build/clone/
update specs never acquire egress flags.

- [ ] **Step 2: Run Podman tests and verify failure**

Run `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`.

Expected: FAIL because runtime specs do not accept egress mounts.

- [ ] **Step 3: Implement `_egress_args` and wrap both agent commands**

Validate absolute run directory, exact project/agent match, and fixed proxy
port. Append `--network=none` before the image name. Mount the runtime at
`/run/agent-egress` read-only and set socket/capability paths only under that
mount. Use:

```text
HTTPS_PROXY=http://127.0.0.1:17843
https_proxy=http://127.0.0.1:17843
HTTP_PROXY=http://127.0.0.1:17843
http_proxy=http://127.0.0.1:17843
NO_PROXY=localhost,127.0.0.1,::1
no_proxy=localhost,127.0.0.1,::1
```

- [ ] **Step 4: Write failing lifecycle ordering and cleanup tests**

Extend the current nested context-manager tests to cover all combinations of
GitHub broker, Claude handover broker, and egress gateway. Assert egress
startup happens before runtime spec construction, invalid policy/adapter probe
prevents gateway and Podman, gateway enter failure prevents Podman, Podman
nonzero cleans every entered context, and cleanup failure returns nonzero
without unrestricted retry.

- [ ] **Step 5: Implement lifecycle composition**

Load policy once during preflight. For enabled policy, create the agent-specific
gateway and enter it with `ExitStack` alongside existing brokers. Pass the
immutable mount to the selected runtime spec. Do not catch a gateway failure
and call the builder again without egress.

- [ ] **Step 6: Run focused regression tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_podman \
  tests.container.test_agentctl -v
bin/lint
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py \
  tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: enforce allowlisted agent runtime egress"
```

---

### Task 7: Real socket/Podman tests, operator documentation, and release evidence gates

**Files:**
- Create: `tests/integration/test_egress_broker_socket.py`
- Create: `tests/integration/test_egress_podman.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Create: `docs/egress-domain-allowlist.md`
- Create: `docs/egress-domain-allowlist-smoke-test.md`
- Modify: `docs/phase1-codex-container.md`
- Modify: `docs/phase2-claude-code.md`
- Modify: `docs/phase3-github-broker.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Produces: automated real Unix-socket and rootless Podman enforcement evidence.
- Produces: bounded operator enable/add/remove/disable/doctor/run/smoke procedures.
- Does not produce: managed production core domains without separately approved real-host evidence.

- [ ] **Step 1: Write failing real Unix-socket integration tests**

Use a local TLS fixture and injected resolver mapping to a test-only loopback
target; the production resolver must continue rejecting loopback. Cross the
real adapter/gateway socket, validate opaque bytes, project/capability/replay/
domain/port denials, one failure followed by success, cleanup, stale client,
and redacted audit.

- [ ] **Step 2: Run socket integration in a capable environment**

Run:

```bash
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src \
python3 -m unittest tests.integration.test_egress_broker_socket -v
```

Expected before completing implementation: FAIL. If the current sandbox denies
Unix socket bind, record `not run` here and require CI/host evidence; do not
change the test to skip an actual product failure.

- [ ] **Step 3: Write rootless Podman enforcement tests**

Build a local test image/fixture network and assert direct DNS, IPv4, IPv6,
UDP, HTTP, unapproved HTTPS, and proxy-variable-unset paths fail while the
approved local TLS fixture succeeds through the gateway. Assert missing
adapter, invalid policy, gateway start failure, and gateway death never launch
or relaunch unrestricted runtime. Keep this test free of public internet and
credentials.

- [ ] **Step 4: Wire CI integration jobs**

Add the egress socket module to the existing broker socket command. Add the
Podman egress module after the base image build, using exact environment gates
and existing pinned versions. Do not add `continue-on-error`.

- [ ] **Step 5: Write failing documentation contracts**

Require README/operator/smoke text for opt-in, exact-domain restrictions,
private state, `--network=none`, TLS opacity, no fallback, warning/PASS/FAIL,
rollback consequence, excluded build/auth/update scope, local-doctor limits,
and `PASS/PARTIAL/FAIL/not run` evidence vocabulary. Require old phase docs to
describe their former warning as historical rather than current universal
behavior.

- [ ] **Step 6: Write operator and smoke documentation**

Document exact commands without example credentials:

```bash
agentctl project configure-egress PROJECT --enable
agentctl project configure-egress PROJECT --add-domain pypi.org
agentctl doctor PROJECT
agentctl run PROJECT
agentctl project configure-egress PROJECT --remove-domain pypi.org
agentctl project configure-egress PROJECT --disable
```

The smoke guide must stop before each real external operation for fresh
approval; prohibit shell tracing, token/header/body/TLS/DNS dump capture,
fallback, retry, and unobserved PASS claims.

- [ ] **Step 7: Run the complete automated verification available locally**

Run:

```bash
bin/lint
PYTHONPATH=src python3 -m unittest discover -s tests/codex -v
PYTHONPATH=src python3 -m unittest discover -s tests/container -v
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src python3 -m unittest \
  tests.integration.test_github_broker_socket \
  tests.integration.test_handover_broker_socket \
  tests.integration.test_egress_broker_socket -v
git diff --check
```

Expected: lint and unit suites PASS. Socket results must be PASS from a capable
environment or explicitly remain unverified for CI; no environmental error is
converted into a product PASS.

- [ ] **Step 8: Run rootless Podman integration in a capable environment**

Run the CI-equivalent image build and:

```bash
AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 \
AGENT_CONTAINER_INTEGRATION_BASE_IMAGE=localhost/agent-container:egress-test \
PYTHONPATH=src python3 -m unittest \
  tests.integration.test_project_image_podman \
  tests.integration.test_egress_podman -v
```

Expected: PASS. If Podman is unavailable, record `not run` and rely on CI or
an approved host; do not weaken the gate.

- [ ] **Step 9: Commit Task 7**

```bash
git add .github/workflows/ci.yml README.md \
  docs/egress-domain-allowlist.md \
  docs/egress-domain-allowlist-smoke-test.md \
  docs/phase1-codex-container.md docs/phase2-claude-code.md \
  docs/phase3-github-broker.md tests/container/test_docs.py \
  tests/integration/test_egress_broker_socket.py \
  tests/integration/test_egress_podman.py
git commit -m "docs: add runtime egress enforcement gates"
```

---

## Post-implementation review and real-host phase

- [ ] Request an independent code/security review covering credentials,
  network bypass, DNS rebinding, proxy parsing, socket authorization, limits,
  cleanup, audit redaction, rollback, and unrestricted fallback. Resolve every
  Critical or Important finding before host testing.
- [ ] Push the create-only work branch and open a PR only after local review;
  subsequent fixes require a new branch/PR under the broker's create-only
  policy.
- [ ] Use the smoke guide to obtain fresh approval for bounded Codex managed
  core discovery, then for bounded Claude discovery. Add only observed exact
  domains in a separately reviewed commit and preserve every failed attempt.
- [ ] Rebuild the reviewed image and obtain fresh approval for Codex and Claude
  inference smoke gates. Record cleanup and no-fallback evidence without
  credentials or complete network logs.
- [ ] Require unit, lint, socket, Podman, documentation, CI, independent review,
  and real-host evidence before changing the historical `WARN network-policy`
  release notes or opting the production project in.
