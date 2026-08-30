# Runtime Egress Domain Allowlist Design

## Status

Approved in conversation on 2026-08-30. This design covers the first,
opt-in release of a domain-restricted network path for normal Codex and
Claude runtimes. It does not change image builds, authentication flows,
explicit update commands, GitHub App broker host traffic, or project
registration and clone traffic.

## Problem

Agent runtimes currently use ordinary rootless Podman networking. Filesystem,
credential, process, mount, and GitHub broker boundaries are narrow, but an
agent process can open an outbound connection to any destination reachable by
the host. `agentctl doctor` therefore reports `WARN network-policy`.

An environment-variable-only proxy is not a security boundary: a process can
unset the variable, use a proxy-unaware client, connect to a literal IP
address, or resolve and connect independently. Rootless firewall rules based
on resolved IP addresses are also a poor match for a domain policy because
CDN addresses change and DNS answers can be ambiguous or rebound.

## Goals

- Give opted-in Codex and Claude runtimes no direct IP network path.
- Permit outbound TLS tunnels only to exact, approved DNS names on TCP port
  443.
- Keep TLS end-to-end between the client in the agent container and the
  destination. The host gateway must not read request bodies, response bodies,
  authentication headers, or TLS plaintext.
- Combine an agent-specific managed core policy with project-specific exact
  domains stored in private host state.
- Fail closed on invalid configuration, gateway failure, DNS ambiguity,
  unsafe destination addresses, malformed requests, exhausted limits, or
  cleanup failure. Never fall back automatically to unrestricted networking.
- Preserve the existing project, agent, credential, mount, GitHub broker,
  handover broker, audit, and runtime cleanup boundaries.
- Roll out explicitly to existing projects and retain the current warning for
  projects that have not opted in.

## Non-goals

- Restricting network access during `agentctl build`, project image builds,
  `agentctl auth`, `agentctl superpowers update`, setup, project registration,
  or broker clone.
- Replacing the GitHub App broker or sending its credentials through the
  egress gateway.
- Inspecting HTTP paths, methods, headers, bodies, certificates, or TLS
  plaintext.
- Supporting plaintext HTTP, UDP, QUIC, arbitrary ports, wildcard domains,
  CIDR ranges, literal IP destinations, or transparent interception.
- Making restricted networking the default for new projects in the first
  release. Default-on is a separate decision after automated and real-host
  gates pass.
- Claiming that an allowed third-party service cannot proxy or redirect work
  at its application layer. The boundary controls the network destination,
  not the semantics of an approved service.

## Chosen Architecture

An opted-in runtime uses `podman run --network=none`. Podman creates a network
namespace without external connectivity, so bypassing proxy configuration
does not restore a direct route. A container-local adapter listens only on the
runtime namespace loopback interface. It accepts a bounded HTTP `CONNECT`
request, authenticates to a project- and runtime-specific host gateway over a
read-only mounted Unix socket, and relays opaque tunnel bytes.

The data path is:

```text
Codex / Claude / shell
        | HTTPS_PROXY
        v
container loopback CONNECT adapter
        | project/runtime Unix socket + capability
        v
host egress gateway
        | exact-domain policy + safe DNS/address checks
        v
approved TCP/443 destination (TLS remains end-to-end)
```

The adapter and gateway use a fixed, versioned framing protocol. The adapter
does not accept a socket path, project ID, policy, capability path, target
port, or upstream address from command-line input supplied by the workspace.
Those values are fixed by the launcher. The workspace receives only standard
proxy environment variables pointing at the loopback listener.

## Components

### Egress policy

`agent_container.egress_policy` owns parsing, canonicalization, persistence,
and policy decisions. The persisted project file has this exact logical
schema:

```json
{
  "mode": "allowlist",
  "additional_domains": [
    "files.pythonhosted.org",
    "pypi.org"
  ],
  "version": 1
}
```

The file is a regular, non-symlink file owned by the invoking user with mode
`0600`. Its parent project directory retains the existing private-directory
contract. Unknown, missing, duplicate, or incorrectly typed fields fail
closed. Writes use a same-directory private temporary file, file `fsync`,
atomic replacement, and parent-directory `fsync`. Failure preserves the
previous valid bytes and removes only the exact temporary file created by the
operation.

Each domain is canonical lowercase ASCII and must satisfy all of these rules:

- total encoded length is 1 through 253 bytes;
- it contains at least two labels separated by one ASCII dot;
- each label is 1 through 63 bytes, begins and ends with an ASCII letter or
  digit, and otherwise contains only ASCII letters, digits, or hyphens;
- it is already lowercase and has no trailing dot;
- it is not an IPv4 or IPv6 literal, `localhost`, or a name ending in
  `.localhost`, `.local`, `.internal`, `.home`, or `.arpa`;
- it contains no wildcard, underscore, whitespace, control character, URL
  scheme, userinfo, path, query, fragment, or port;
- its canonical value is not duplicated.

Internationalized names are not accepted in the first release. Operators
must supply an already reviewed ASCII A-label if support is added in a later
design; this design rejects `xn--` labels as well as non-ASCII input to avoid
introducing an unreviewed homograph boundary.

The stored list is sorted and contains no managed core entries. Managed core
domains are versioned application constants selected by agent type. They are
not editable through project configuration. The first production values must
come from bounded, credential-safe real-host observation of current Codex and
Claude startup and one inference operation. A domain is not added merely
because it appears plausible in documentation or prior logs.

### Configuration CLI

The existing `agentctl project` command gains a `configure-egress`
subcommand:

```text
agentctl project configure-egress PROJECT --enable
agentctl project configure-egress PROJECT --add-domain DOMAIN
agentctl project configure-egress PROJECT --remove-domain DOMAIN
agentctl project configure-egress PROJECT --disable
```

Exactly one action is accepted per invocation. `--add-domain` and
`--remove-domain` require an already enabled valid policy. Adding a managed
core domain, adding a duplicate, or removing an absent domain is an error and
does not rewrite the file. `--enable` creates the empty version 1 policy and
refuses to overwrite any existing path. `--disable` removes only an exact,
revalidated version 1 policy after printing that the next runtime will regain
unrestricted outbound networking; it never follows symlinks or removes an
unknown schema. A failed action does not print the policy body or private
paths.

Project policy is host-private state. Repository content, agent output,
environment variables, and mounted workspace files cannot add domains or
enable the policy.

### Runtime session and gateway

`agent_container.egress_broker_runtime` creates a project-scoped private
runtime directory using the established GitHub and handover broker patterns.
It generates an unguessable runtime capability, opens the Unix listener before
starting Podman, and returns an immutable mount description. Runtime directory
and capability files use the existing `0700` directory and `0600` regular-file
contracts. The mounted directory is read-only in the container.

The gateway validates the Unix peer UID, protocol version, capability,
project, monotonically increasing request sequence, operation, target domain,
and fixed target port before performing DNS or TCP work. A failed authorization
request cannot be replayed and cannot select another project or runtime.

The first implementation has conservative fixed limits:

- maximum CONNECT request header: 16 KiB;
- maximum 32 concurrent tunnels per runtime;
- maximum 128 successful tunnel creations per runtime;
- DNS and TCP connect timeout: 15 seconds each;
- tunnel idle timeout: 300 seconds;
- maximum tunnel lifetime: 7,200 seconds;
- maximum bytes in either direction per tunnel: 2 GiB;
- relay read chunk: 64 KiB.

Limits are checked independently. Reaching one closes only that tunnel unless
the listener or worker infrastructure itself is unhealthy. The gateway never
retries a destination connection and never switches to a different resolved
address after a failed connect.

### DNS and destination-address safety

The gateway resolves the approved exact hostname on the host for TCP port 443
with `AF_UNSPEC` and `SOCK_STREAM`. Every returned address must be a globally
routable unicast address. If any answer is loopback, private, link-local,
multicast, unspecified, reserved, documentation-only, carrier-grade NAT, or
otherwise non-global according to the standard library address classifier,
the entire request is denied. Mixed safe and unsafe answer sets are denied;
unsafe answers are never silently discarded.

The gateway de-duplicates the safe answer set and selects the first result in
resolver order for one connection attempt. Immediately after connect it reads
the socket peer address and requires it to equal the selected address. A
mismatch fails closed before returning CONNECT success. The resolved and peer
addresses are neither sent to the agent nor written to audit.

This protects host-local and private-network services from approved-name DNS
rebinding. It does not pin public CDN addresses between tunnels; each new
tunnel receives a new bounded resolution and validation.

### Container adapter and Podman invocation

The image contains a managed `agent-egress-adapter` entry point. The launcher
starts it as part of the runtime command wrapper before the agent CLI. It binds
only `127.0.0.1` and `::1` on a launcher-selected fixed high port, reports
readiness through a private inherited file descriptor, and then drops access
to the readiness descriptor. Failure to bind both loopback families or become
ready prevents the agent CLI from starting.

For allowlist mode, `podman.py` adds:

- `--network=none`;
- the read-only gateway runtime mount;
- fixed socket, capability, project, and proxy environment values;
- `HTTPS_PROXY` and `https_proxy` pointing to the loopback adapter;
- `HTTP_PROXY` and `http_proxy` pointing to the same adapter, which rejects
  non-CONNECT and non-443 requests;
- a fixed `NO_PROXY` and `no_proxy` containing only `localhost,127.0.0.1,::1`.

Caller-supplied proxy variables do not override these values. Unsetting them
inside the runtime cannot create connectivity because the Podman network mode
has no external route. The adapter process shares the agent lifecycle: an
adapter failure terminates the wrapper and therefore the agent runtime.

GitHub and handover Unix-socket mounts remain independent. Egress enablement
does not grant either broker, and those brokers do not grant egress.

### Doctor behavior

`agentctl doctor PROJECT` performs no network request. For an enabled project
it validates the exact policy schema and private filesystem boundary, checks
that the image contains the managed adapter self-check, and verifies the local
host runtime prerequisites. Success renders:

```text
PASS  network-policy: outbound HTTPS uses the project domain allowlist
```

Invalid or unsafe enabled policy is `FAIL`, not `WARN`, and prevents
`agentctl run`. A project without a policy retains:

```text
WARN  network-policy: outbound network is not domain-restricted
```

Doctor does not resolve configured names, connect to them, claim remote
availability, or print managed or additional domain values.

## Protocol and Error Handling

The adapter accepts only syntactically canonical HTTP/1.1 CONNECT authority
form with an exact ASCII hostname and port 443. It rejects absolute-form URLs,
userinfo, literal IPs, alternate ports, duplicate Host headers, transfer
encoding, request bodies, extra requests before tunnel establishment, and
oversized or incomplete headers. It returns a fixed proxy error without
echoing the authority.

The Unix protocol carries bounded length-prefixed metadata followed by an
opaque bidirectional byte stream only after gateway authorization and upstream
connect succeed. Before success, unexpected EOF, trailing fields, unknown
fields, duplicate fields, malformed UTF-8, non-canonical JSON, oversized
frames, or protocol-version mismatch fail closed. No error response includes
the domain, address, policy list, project path, capability, exception text, or
upstream response.

Known per-connection failures use fixed stages such as `authorize`, `resolve`,
`connect`, `relay`, and `limit`. Listener, cleanup, thread-supervision, or
programming failures stop the runtime gateway. The agent launcher receives
only a fixed message and nonzero exit status.

## Audit

Audit uses the existing private append-only JSON-lines conventions. Records
contain only timestamp, project ID, runtime ID, operation `connect`, status
`ok`/`denied`/`error`, a fixed failure stage when applicable, and bounded byte
counts when a tunnel closes. They never contain a hostname, IP address, port,
DNS answer, TLS data, HTTP data, credential, capability, exception, process
arguments, or workspace content.

Operators diagnose an unavailable allowed service through a separately
approved host-side check. They do not weaken audit redaction or enable packet
capture as a fallback.

## Rollout and Compatibility

Existing projects are never migrated implicitly. A project opts in only via
`configure-egress --enable`. Until then, runtime behavior is unchanged and
doctor retains the warning. If an enabled gateway cannot start, the agent
container does not start. If it stops unexpectedly, the adapter and wrapper
terminate; Podman never restarts with ordinary networking.

Explicit `--disable` is the only rollback to unrestricted runtime networking.
It is an operator security-policy change, not automatic recovery. Disabling
does not delete audit records or change GitHub/handover broker policy.

After automated verification and separate real-host smoke gates pass for both
agents, the `agent-container` project may be opted in. Making allowlist mode
the default for newly created projects requires a later design and release
decision with migration documentation.

## Verification

### Unit tests

- Accept valid exact lowercase ASCII domains and deterministic sorting.
- Reject wildcard, IP, local/reserved suffix, Unicode, `xn--`, trailing-dot,
  uppercase, port, URL, path, control, empty-label, overlong-label, overlong
  name, duplicate, unknown-schema, unsafe-file, and unsafe-parent cases.
- Prove atomic writes preserve old bytes and clean exact temporary files on
  write, `fsync`, replace, and parent-`fsync` failures.
- Verify CLI one-action parsing, enable/add/remove/disable state transitions,
  no-op rejection, and secret-safe fixed errors.
- Verify authorization, replay, project, sequence, capability, UID, operation,
  concurrency, count, timeout, lifetime, byte, and frame limits.
- Verify safe public address acceptance and all non-global or mixed DNS answer
  rejection for IPv4 and IPv6.
- Verify connect-peer mismatch, no retry, fixed error stages, bounded audit,
  and absence of domain/IP/body/capability markers.

### Unix-socket integration

- Cross the real adapter-to-gateway socket for one local TLS tunnel without
  exposing plaintext to the gateway.
- Deny a second project, stale capability, replayed sequence, malformed
  request, unapproved domain, non-443 port, and unsafe resolver result.
- Show a known connection failure does not stop the next authorized request.
- Show runtime exit removes socket and capability and a stale client is
  denied without changing audit.

### Podman integration

- With allowlist enabled, direct DNS, direct IPv4, direct IPv6, UDP, plaintext
  HTTP, an unapproved HTTPS name, and a proxy-unaware direct socket all fail.
- Unsetting every proxy environment variable does not restore connectivity.
- An approved local TLS fixture succeeds only through the gateway.
- Codex/Claude launch arguments, workspace, project image, auth, GitHub broker,
  handover broker, resource labels, read-only root, dropped capabilities, and
  no-new-privileges remain intact.
- A missing adapter, invalid policy, gateway startup failure, or gateway death
  never launches or relaunches an unrestricted agent container.

### Documentation and real-host gates

README and an operator guide document exact configuration, warning/PASS/FAIL
meaning, non-goals, rollback consequences, and the difference between local
doctor and remote proof. A dedicated smoke checklist records all observations
as `PASS`, `PARTIAL`, `FAIL`, or `not run` and preserves earlier failures.

Managed core discovery and the final Codex and Claude smoke tests require
fresh approval immediately before external execution. Observation must avoid
shell tracing and must not record tokens, headers, bodies, TLS plaintext,
complete DNS answers, complete network logs, or private state. Each required
domain must be justified by a bounded failed-then-allowed operation. Both
agents must start, perform one inference, use project Git/handover paths as
configured, exit cleanly, and leave no gateway runtime artifacts.

## Baseline and Completion Criteria

The isolated worktree started from `v0.4.0` (`f172113`). Baseline Codex tests
passed 21 tests; container unit tests passed 577 tests with 1 skip. The six
socket integration tests could not bind Unix sockets in the current execution
sandbox and errored before assertions; the latest main CI evidence had passed
the same integration suite. This environmental limitation is not recorded as
a feature PASS.

Implementation is complete only when lint, Codex tests, container tests,
Unix-socket integration, Podman integration, documentation contracts,
`git diff --check`, independent security review, and approved real-host gates
have results recorded without Critical or Important findings. Tests that
cannot run in the current sandbox remain explicitly unverified until CI or an
approved host run supplies evidence.
