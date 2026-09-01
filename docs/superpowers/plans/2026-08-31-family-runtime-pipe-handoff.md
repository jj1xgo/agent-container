# Family Runtime Pipe Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent every Family-enabled container from executing agent-controlled code until its real container PID has been registered, using a fail-closed host-to-launcher pipe gate.

**Architecture:** `run_command_supervised` creates an anonymous pipe, injects the read descriptor into Podman's preserved-FD arguments and the launcher gate argument, then registers the live pidfile PID before writing one release byte. `agent-runtime-launcher` blocks on that descriptor and execs the existing runtime chain only after validating the byte; EOF and malformed input fail closed.

**Tech Stack:** Python 3 standard library (`os`, `subprocess`, `pathlib`, `unittest`), rootless Podman with crun preserved FDs, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-family-runtime-pipe-handoff-design.md`

## Global Constraints

- No agent-controlled command runs before Family broker registration succeeds.
- The gate carries only the fixed byte `b"1"`; it carries no PID, path, capability, credential, repository identity, or agent-controlled content.
- The container receives only the pipe read end, and all failure paths close the host write end without releasing the launcher.
- Family socket mounts remain path-based and read-only; do not add proc-FD bind mounts, writable handshake mounts, or network access.
- Existing PID registration and peer-ancestry validation remain authoritative.
- Permanent errors do not include PID, descriptor, capability, or host path values.
- Final acceptance is Unit success plus Podman `Ran 14 tests`, `OK`, and no `skipped`; do not merge automatically.

---

### Task 1: Fail-closed launcher pipe gate

**Files:**
- Modify: `container/bin/agent-runtime-launcher`
- Test: `tests/container/test_image.py`

**Interfaces:**
- Consumes: launcher argument `--registration-gate-fd=<decimal fd>` and an inherited readable pipe descriptor.
- Produces: a launcher that reads exactly the fixed release byte `b"1"`, closes the gate fd, and only then calls `os.execvp`; malformed argument exits 64 and gate read/EOF/wrong-byte failures exit 70.

- [ ] **Step 1: Write failing launcher gate tests**

Add real subprocess tests to `ContainerImageContractTest`. The success test starts the launcher with the read end, uses a child command that creates a marker, confirms the marker is absent before release, writes `b"1"`, and confirms exit 0 and marker creation. Add subtests that pass a malformed fd argument, close the write end without a byte, or write `b"0"`; assert 64 for malformed syntax and 70 for gate failures.

```python
def test_runtime_launcher_waits_for_registration_gate_before_exec(self) -> None:
    with TemporaryDirectory() as temp:
        marker = Path(temp) / "executed"
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            (
                str(ROOT / "container/bin/agent-runtime-launcher"),
                f"--registration-gate-fd={read_fd}",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; Path(sys.argv[1]).touch()",
                str(marker),
            ),
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        try:
            self.assertIsNone(process.poll())
            self.assertFalse(marker.exists())
            self.assertEqual(os.write(write_fd, b"1"), 1)
        finally:
            os.close(write_fd)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(marker.exists())
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image.ContainerImageContractTest.test_runtime_launcher_waits_for_registration_gate_before_exec tests.container.test_image.ContainerImageContractTest.test_runtime_launcher_rejects_invalid_registration_gate -v`

Expected: FAIL because the launcher rejects the new argument or executes before the byte arrives.

- [ ] **Step 3: Implement the minimal launcher gate**

Remove `signal` and the `--registration-stop` branch. Before processing `--close-fd`, parse the optional Family gate argument and block on it:

```python
if arguments and arguments[0].startswith("--registration-gate-fd="):
    match = re.fullmatch(r"--registration-gate-fd=([0-9]+)", arguments.pop(0))
    if match is None or int(match.group(1)) < 3:
        raise SystemExit(64)
    gate_fd = int(match.group(1))
    try:
        release = os.read(gate_fd, 2)
        os.close(gate_fd)
    except OSError:
        raise SystemExit(70) from None
    if release != b"1":
        raise SystemExit(70)
```

Keep the existing `--close-fd` processing and final `--`/command validation after the gate.

- [ ] **Step 4: Run launcher tests and verify GREEN**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image.ContainerImageContractTest.test_runtime_launcher_waits_for_registration_gate_before_exec tests.container.test_image.ContainerImageContractTest.test_runtime_launcher_rejects_invalid_registration_gate tests.container.test_image.ContainerImageContractTest.test_runtime_launcher_closes_exact_fd_before_agent_exec -v`

Expected: 3 tests PASS with no warnings.

- [ ] **Step 5: Commit the launcher gate**

```bash
git add container/bin/agent-runtime-launcher tests/container/test_image.py
git commit -m "fix: gate family launcher on host release"
```

### Task 2: Supervisor registration and pipe release ordering

**Files:**
- Modify: `src/agent_container/podman.py:275-325, 810-890`
- Test: `tests/container/test_podman.py:50-230`

**Interfaces:**
- Consumes: a Family-enabled `CommandSpec` containing one launcher placeholder argument `--registration-gate`.
- Produces: `_family_gate_argv(argv: tuple[str, ...], gate_fd: int, pidfile: Path) -> tuple[str, ...]`, `_wait_for_container_pid(pidfile: Path, process: subprocess.Popen[str]) -> int`, and supervisor ordering `Popen -> live PID -> register_runtime -> os.write(b"1") -> close`.

- [ ] **Step 1: Write failing argv and live-PID tests**

Replace stopped-state tests with literal behavior tests:

```python
def test_family_gate_argv_preserves_exact_fd_and_replaces_placeholder(self) -> None:
    argv = _family_gate_argv(
        ("podman", "run", "image", "agent-runtime-launcher",
         "--registration-gate", "--", "codex"),
        19,
        Path("/private/container.pid"),
    )
    self.assertEqual(argv[:4], (
        "podman", "run", "--pidfile=/private/container.pid", "--preserve-fd=19"
    ))
    self.assertEqual(argv[5:8], (
        "agent-runtime-launcher", "--registration-gate-fd=19", "--"
    ))

def test_family_pid_wait_accepts_live_nonstopped_container(self) -> None:
    # pidfile returns 123 and /proc stat contains state S; expect 123 immediately.
```

Also test duplicate/missing placeholder rejection and early Podman exit without rendering PID or path.

- [ ] **Step 2: Run focused helper tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman.PodmanCommandTest.test_family_gate_argv_preserves_exact_fd_and_replaces_placeholder tests.container.test_podman.PodmanCommandTest.test_family_pid_wait_accepts_live_nonstopped_container -v`

Expected: FAIL because `_family_gate_argv` and `_wait_for_container_pid` do not exist.

- [ ] **Step 3: Implement argv injection and live PID discovery**

Change `_runtime_launcher_args` to emit `--registration-gate` for Family mounts. Implement `_family_gate_argv` with these exact validations: argv begins `("podman", "run")`; `gate_fd >= 3`; the placeholder occurs exactly once immediately after `agent-runtime-launcher`; then insert `--pidfile=<path>` and `--preserve-fd=<gate_fd>` after `podman run` and replace only the placeholder with `--registration-gate-fd=<gate_fd>`.

Rename `_wait_for_stopped_container` to `_wait_for_container_pid`. Retain the 10-second bounded wait, positive decimal pid validation, `/proc/<pid>/stat` parsing, and early-process-exit handling, but return any live process with a valid stat record rather than requiring state `T`. Use fixed safe failure stages only.

- [ ] **Step 4: Write failing supervisor ordering and failure-cleanup tests**

Update `test_family_supervision_registers_stopped_runtime_before_resume` to `test_family_supervision_registers_runtime_before_gate_release`. Patch `os.pipe` to return `(88, 89)`, `os.close` and `os.write` to record events, and `Popen` to avoid real invalid descriptors. Assert this order:

```python
[
    "revalidated",
    ("closed", 88),
    ("pid-ready", "container.pid", 4321),
    ("registered", 9876),
    ("released", 89, b"1"),
    ("closed", 89),
]
```

Assert launched argv contains both `--preserve-fd=88` and `--registration-gate-fd=88`, and `Popen(pass_fds=(*spec.pass_fds, 88))`. In registration-failure and process-start-failure tests, assert fd 89 is closed and `os.write` is never called.

- [ ] **Step 5: Run supervisor tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman.PodmanCommandTest.test_family_supervision_registers_runtime_before_gate_release tests.container.test_podman.PodmanCommandTest.test_family_registration_failure_closes_gate_without_release -v`

Expected: FAIL because the supervisor still sends `SIGCONT` and owns no gate pipe.

- [ ] **Step 6: Implement supervisor pipe ownership**

For Family runs, create `read_fd, write_fd = os.pipe2(os.O_CLOEXEC)`, build the dynamic argv with `_family_gate_argv`, and call `Popen` with `pass_fds=(*spec.pass_fds, read_fd)`. Close the host read end immediately after successful `Popen`. After `_wait_for_container_pid` and `family_runtime.register_runtime(pid)`, require `os.write(write_fd, b"1") == 1`, then close the write end and clear its ownership variable. In `finally`, close each still-owned descriptor once. Remove `os.kill(..., SIGCONT)`.

If `pipe2` is unavailable on a supported platform, do not add fallback behavior; this runtime already requires Linux `/proc`, Unix sockets, Podman, and crun.

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected in a normal Linux environment: all tests PASS. In this workspace, separately record the known sandbox-only Unix socket bind `EPERM` if it remains the sole failure, then run all non-socket focused tests explicitly and require PASS.

- [ ] **Step 8: Commit supervisor handoff**

```bash
git add src/agent_container/podman.py tests/container/test_podman.py
git commit -m "fix: release family runtime through pipe gate"
```

### Task 3: Real Podman contract, diagnostic cleanup, and CI acceptance

**Files:**
- Modify: `tests/integration/test_family_intake_podman.py:390-430, 500-610`
- Modify: `src/agent_container/podman.py:285-330` only if temporary detailed observations remain
- Test: `tests/integration/test_family_intake_podman.py`

**Interfaces:**
- Consumes: the pipe-gated Family `CommandSpec` and supervisor behavior from Tasks 1-2.
- Produces: fixture assertions for the static `--registration-gate` placeholder, removal of temporary process-reader counting, and final 14-test real Podman evidence.

- [ ] **Step 1: Update the static command contract test**

For all four Codex/Claude and egress/no-egress specs, keep `spec.pass_fds == ()` before supervision and assert the launcher slice is:

```python
("agent-runtime-launcher", "--registration-gate", "--")
```

Assert no dynamic `--registration-gate-fd=` or `--preserve-fd=` appears in the static spec; those are supervisor-owned runtime details.

- [ ] **Step 2: Remove temporary process-reader diagnostics**

Delete `process_reads`, `record_process_read`, the reader replacement, and the `AssertionError("family peer validation process reads: ...")` wrapper. Let the original runtime exception retain its safe fixed stage. Reduce `_wait_for_container_pid` diagnostics to permanent safe stages if any diagnostic-only branches from commits `3374e24` and `9086076` are no longer operationally useful.

- [ ] **Step 3: Run local verification**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_image -v
PYTHONPATH=src python3 -m unittest tests.container.test_podman -v
PYTHONPATH=src python3 -m unittest tests.integration.test_family_intake_podman.FamilyIntakePodmanFixtureTest -v
bin/lint
git diff --check
```

Expected: all tests and checks PASS except only a separately identified sandbox Unix-socket-bind `EPERM`; no test may skip or fail for a code reason.

- [ ] **Step 4: Commit fixture and diagnostic cleanup**

```bash
git add tests/integration/test_family_intake_podman.py src/agent_container/podman.py
git commit -m "test: finalize family pipe handoff contract"
```

- [ ] **Step 5: Push and inspect the exact CI gate**

Push `fix/family-podman-ci-gate`, wait for the PR-head workflow, and inspect the Podman job log rather than relying only on the check name.

Required evidence:

```text
Unit tests: SUCCESS
Ran 14 tests
OK
no occurrence of skipped
```

If CI fails, use `superpowers:systematic-debugging`, form one evidence-backed hypothesis, and do not stack another fix. Do not merge the PR.

- [ ] **Step 6: Request code review after CI success**

Use `superpowers:requesting-code-review` against the final branch diff. Address only verified findings, rerun the full acceptance gate after changes, and present the exact run/job URLs and log evidence to the user before asking for merge permission.
