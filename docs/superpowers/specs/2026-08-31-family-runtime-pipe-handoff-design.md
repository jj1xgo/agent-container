# Family Runtime Pipe Handoff Design

## Context

The Family intake broker must register the real container process before any
agent-controlled command can reach the mounted intake socket. The current
launcher tries to stop itself with `SIGSTOP`, while the host reads Podman's
`--pidfile`, registers that PID, and resumes it.

Real rootless Podman CI showed that the pidfile contains a live container PID,
but the process never enters the stopped state and continues into the probe
command. The launcher is PID 1 in its PID namespace. Linux gives namespace PID
1 special signal semantics, so a self-sent `SIGSTOP` does not provide the
required startup barrier. Host-side inspection and Podman lifecycle commands
also cannot be used concurrently as a reliable barrier because they contend
with the attached `podman run` lifecycle.

## Decision

Replace the signal-stop barrier with a one-way anonymous pipe owned by the
host supervisor.

For a Family-enabled run, the supervisor creates a pipe before launching
Podman. The read descriptor is inherited by Podman and explicitly preserved
into the OCI process using Podman's crun-supported preserved-FD interface. The
launcher blocks on that descriptor before it closes any other preserved
descriptor or executes the agent-controlled command.

The host waits for Podman's pidfile to contain a live container PID. It then
registers that PID with the Family broker and writes one fixed release byte to
the pipe. Only that byte permits the launcher to close the gate descriptor and
execute the requested command. EOF, an invalid descriptor, an unexpected byte,
or registration failure is fatal and never starts the user command.

The pipe is only a local ordering primitive. It carries no capability, PID,
path, credential, repository identity, or agent-controlled content.

## Ordering and ownership

The required successful order is:

1. Host creates the pipe and starts `podman run` with a pidfile and preserved
   read descriptor.
2. Container launcher blocks reading the pipe.
3. Host reads and validates the live pidfile PID.
4. Family broker registers that PID.
5. Host writes the fixed release byte and closes its write descriptor.
6. Launcher validates the byte, closes the read descriptor, and `exec`s the
   existing runtime chain.

The supervisor owns both pipe ends until process creation succeeds. It closes
its copy of the read end immediately after `Popen`. Every exit path closes all
remaining ends. If setup, PID discovery, or registration fails, the write end
is closed without a release byte, so the launcher observes EOF and cannot run
agent code. Existing named-container cleanup remains responsible for the
Podman/container lifecycle.

The gate descriptor must not collide with existing preserved descriptors.
Command construction carries the exact descriptor number rather than assuming
it is fd 3. Family socket mounts remain path-based and read-only; this design
does not reintroduce proc-FD socket bind mounts.

## Error handling and diagnostics

The launcher returns a fixed nonzero status for a malformed gate argument,
read error, EOF, or wrong release byte. It does not print descriptor numbers or
host paths. The host reports bounded stages such as pidfile unavailable,
process unavailable, registration failed, or gate release failed without
including PID, capability, or path values.

The temporary process-reader counter and detailed pidfile observation added
while diagnosing CI are removed once the pipe protocol succeeds. Permanent
errors retain only the minimum stage information needed to operate the system.

## Security properties

- No agent-controlled command runs before broker registration succeeds.
- The container cannot manufacture the host's release byte because it receives
  only the pipe's read end.
- No credential or Family repository identity crosses the container boundary.
- Registration continues to bind the broker session to the real container PID
  and the existing peer-ancestry checks remain unchanged.
- Failure is closed: closing or losing the gate prevents command execution.
- The change adds no network access and no fallback to host `gh`, credentials,
  or a less restrictive broker path.

## Testing and acceptance

TDD covers launcher rejection of missing, malformed, EOF, and incorrect gates;
successful release followed by exact command execution; supervisor descriptor
construction and closure; registration-before-release ordering; and failure
cleanup without release.

Local verification runs the focused launcher and Podman supervisor unit tests,
the broader container tests that do not require unavailable sandbox socket
operations, `bin/lint`, and `git diff --check`.

The final acceptance gate is GitHub Actions rootless Podman CI on the PR head:
the Unit job succeeds, the Podman log says `Ran 14 tests` and `OK`, and the log
contains no `skipped`. The PR is not merged automatically.

## Rejected alternatives

- A handled signal gate has a readiness race between pidfile publication and
  signal-handler installation, requiring another handshake.
- Host `SIGSTOP` after pidfile discovery permits agent code to run during the
  discovery race.
- A socket or filesystem handshake is larger, requires another mounted
  writable object, and repeats bind-mount portability problems already observed
  with crun.
- Capability-only initial registration weakens the existing process ancestry
  boundary and is unnecessary when a local one-way pipe can preserve it.
