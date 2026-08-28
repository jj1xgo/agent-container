# GitHub Issue Read-Only Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add fixed-schema, project-scoped `agent-github issue list` and `agent-github issue view NUMBER` operations without exposing credentials or adding Issue write capabilities.

**Architecture:** Add a dedicated GET-only `github_issue.py` transport and validators, then route two new broker operations through the existing authenticated Unix-socket session. Keep the validated PR implementation unchanged, share only the installation token provider, and emit bounded JSON plus secret-free Issue audit metadata.

**Tech Stack:** Python 3.11+, standard-library `urllib`, Unix sockets, `unittest`, rootless Podman integration tests.

**Spec:** `docs/superpowers/specs/2026-08-28-github-issue-read-only-design.md`

## Global Constraints

- Allowed commands are exactly `agent-github issue list` and `agent-github issue view NUMBER`.
- Allowed broker operations are exactly `issue-list` with `{}` and `issue-view` with `{"number": INT}`.
- List is fixed to open Issues, newest-created first, maximum 30; no user-controlled filters, search, pagination, query, owner, repository, URL, or headers.
- View returns body but no comments. No create, edit, comment, close, reopen, lock, delete, label mutation, or assignee mutation operation exists.
- Installation tokens remain exact-repository tokens and add only `issues: read` to the existing permissions.
- Issue title maximum is 256 UTF-8 bytes; body maximum is 256 KiB; each label name maximum is 100 UTF-8 bytes; label count maximum is 100; serialized response maximum is 2 MiB.
- Client, stderr, audit, and exceptions never expose raw GitHub responses, token, JWT, private key, capability, Authorization header, Issue title/body/author/labels/URL, or environment values.
- Existing Git transport and PR create/view/checks behavior remains unchanged.
- Every implementation task follows strict RED-GREEN-REFACTOR and ends with focused tests and a commit.

---

### Task 1: Extend policy, token permission, failure stage, and audit schema

**Files:**
- Modify: `src/agent_container/github_broker_policy.py`
- Modify: `src/agent_container/github_app.py`
- Modify: `src/agent_container/github_broker_error.py`
- Modify: `src/agent_container/github_broker.py`
- Test: `tests/container/test_github_broker_policy.py`
- Test: `tests/container/test_github_app.py`
- Test: `tests/container/test_github_broker.py`

**Interfaces:**
- Produces: `validate_issue_number(value: int) -> int`.
- Produces: `BrokerPolicy.validate_operation()` acceptance of `issue-list` and `issue-view` only.
- Produces: exact token permissions containing `"issues": "read"`.
- Produces: `BrokerSession.audit(..., issue_number: int | None = None)` and failure stage `issue-request`.
- Consumes: existing project/repository policy, private audit writer, and installation token validation.

- [ ] **Step 1: Write failing policy and audit tests**

Add tests that require the new operations, reject adjacent names, validate Issue numbers, and keep Issue audit data distinct from PR data:

```python
def test_allows_only_fixed_issue_read_operations(self) -> None:
    for operation in ("issue-list", "issue-view"):
        self.assertEqual(self.policy.validate_operation(operation), operation)
    for operation in (
        "issue-create", "issue-edit", "issue-comment", "issue-close",
        "issue-lock", "issue-delete", "issue-search", "issue-query",
    ):
        with self.assertRaises(ValueError):
            self.policy.validate_operation(operation)

def test_validates_issue_number_without_accepting_boolean(self) -> None:
    self.assertEqual(validate_issue_number(1), 1)
    self.assertEqual(validate_issue_number(2_147_483_647), 2_147_483_647)
    for value in (True, 0, -1, 2_147_483_648, "1"):
        with self.subTest(value=value), self.assertRaises(ValueError):
            validate_issue_number(value)  # type: ignore[arg-type]

def test_audits_issue_number_separately(self) -> None:
    self.session.audit(operation="issue-view", status="ok", issue_number=12)
    record = json.loads(self.session.audit_file.read_text(encoding="utf-8"))
    self.assertEqual(record["issue_number"], 12)
    self.assertNotIn("pr_number", record)
```

- [ ] **Step 2: Write failing installation-token permission tests**

Update the valid fixture and expected request body to require exactly:

```python
"permissions": {
    "checks": "read",
    "contents": "write",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "write",
}
```

Add subtests proving missing, extra, and `issues: write` permissions are rejected without including response markers in exceptions.

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_policy \
  tests.container.test_github_app \
  tests.container.test_github_broker -v
```

Expected: FAIL because Issue operations, `validate_issue_number`, `issues: read`, `issue-request`, and `issue_number` do not exist.

- [ ] **Step 4: Implement the minimal policy and audit changes**

In `github_broker_policy.py` add both operation names and:

```python
MAX_ISSUE_NUMBER = 2_147_483_647

def validate_issue_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("issue number is invalid")
    if not 1 <= value <= MAX_ISSUE_NUMBER:
        raise ValueError("issue number is invalid")
    return value
```

Add `"issues": "read"` to `_TOKEN_PERMISSIONS`, add `"issue-request"` to `BROKER_FAILURE_STAGES`, and extend `BrokerSession.audit` with `issue_number`. Validate it with `validate_issue_number` and serialize only as `record["issue_number"]`.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run the Step 3 command.

Expected: PASS with zero failures.

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/github_broker_policy.py \
  src/agent_container/github_app.py \
  src/agent_container/github_broker_error.py \
  src/agent_container/github_broker.py \
  tests/container/test_github_broker_policy.py \
  tests/container/test_github_app.py \
  tests/container/test_github_broker.py
git commit -m "feat: add GitHub Issue broker policy"
```

---

### Task 2: Add the dedicated GET-only GitHub Issue transport

**Files:**
- Create: `src/agent_container/github_issue.py`
- Create: `tests/container/test_github_issue.py`

**Interfaces:**
- Consumes: `BrokerPolicy`, `validate_issue_number`, `InstallationTokenProvider`, `InstallationToken`, `HttpResponse`, and `BrokerStageError`.
- Produces: `MAX_ISSUE_RESPONSE_BYTES = 2 * 1024 * 1024`.
- Produces: `github_issue_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse`.
- Produces: `GitHubIssueTransport(policy, tokens, transport=github_issue_transport)` with `list_open() -> dict[str, object]` and `view(number: int) -> dict[str, object]`.

- [ ] **Step 1: Write failing happy-path tests with literal GitHub fixtures**

Build complete fixtures with required GitHub fields plus unrelated fields that must not escape. Assert exact reduced output:

```python
def test_lists_open_issues_in_response_order_and_excludes_pull_requests(self) -> None:
    response = json_response([
        issue_payload(3, title="Newest"),
        issue_payload(2, title="PR", pull_request={"url": "ignored"}),
        issue_payload(1, title="Oldest", labels=[{"name": "bug", "color": "f00"}]),
    ])
    transport, calls = subject(response)
    self.assertEqual(
        transport.list_open(),
        {"issues": [
            issue_summary(3, "Newest"),
            issue_summary(1, "Oldest", labels=["bug"]),
        ]},
    )
    self.assertEqual(calls[0][0], "GET")
    self.assertTrue(calls[0][1].endswith(
        "/issues?state=open&per_page=30&sort=created&direction=desc"
    ))
    self.assertIsNone(calls[0][3])

def test_views_issue_with_body_and_normalizes_null_body(self) -> None:
    transport, _ = subject(json_response(issue_payload(12, body=None)))
    result = transport.view(12)
    self.assertEqual(result["body"], "")
    self.assertEqual(set(result), {
        "number", "title", "state", "author", "labels", "body",
        "created_at", "updated_at", "url",
    })
```

- [ ] **Step 2: Run happy-path tests to verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_issue -v
```

Expected: ERROR importing `agent_container.github_issue`.

- [ ] **Step 3: Implement minimal GET transport and valid response reducer**

Create `github_issue.py` with fixed constants and methods. Generate paths only from `policy.repository.slug` and validated number. Use Issue-specific generic errors and `BrokerStageError("issue-request")`.

The reducer emits literal allowlisted keys:

```python
def _summary(policy: BrokerPolicy, payload: dict[str, object]) -> dict[str, object]:
    number = validate_issue_number(payload.get("number"))  # type: ignore[arg-type]
    # Validate each local below; never copy payload wholesale.
    return {
        "number": number,
        "title": title,
        "state": state,
        "author": author,
        "labels": labels,
        "created_at": created_at,
        "updated_at": updated_at,
        "url": html_url,
    }
```

Do not import private helpers from `github_pr.py`; bounded GET/JSON duplication is intentional isolation.

- [ ] **Step 4: Run happy-path tests to verify GREEN**

Run the Step 2 command.

Expected: PASS for list order, PR exclusion, endpoint, fixed fields, and null-body normalization.

- [ ] **Step 5: Write failing validation, retry, and non-logging tests**

Use literal boundary fixtures and subtests for:

```python
invalid_changes = (
    {"number": True}, {"number": 0}, {"title": ""},
    {"title": "x" * 257}, {"state": "merged"}, {"user": {}},
    {"labels": [{}]}, {"created_at": "2026-08-28"},
    {"html_url": "https://github.com/other/repo/issues/12"},
)
```

Also cover 31 array items, 101 labels, 101-byte UTF-8 label, 256-KiB body acceptance, one-byte-over rejection, non-JSON, wrong content type, redirect, more than 2 MiB, and secret markers in rejected bodies. Prove one retry on 401 and no retry on 403 or 500.

- [ ] **Step 6: Run validation tests to verify RED**

Run the Step 2 command.

Expected: FAIL on the first unimplemented bound, schema, URL, or retry assertion.

- [ ] **Step 7: Implement strict validation and bounded retry**

Add focused helpers for text, UTC timestamp, labels, author, URL, list JSON, and object JSON. Read at most `MAX_ISSUE_RESPONSE_BYTES + 1`, reject redirects, and convert failures to `BrokerStageError("issue-request")` without raw message chaining.

Retry skeleton:

```python
for attempt in range(2):
    token = _get_installation_token(self.tokens)
    response = self.transport("GET", url, headers(token), None)
    if response.status != 401 or attempt == 1:
        return response
    _invalidate_installation_token(self.tokens)
raise AssertionError("unreachable")
```

- [ ] **Step 8: Run transport tests to verify GREEN**

Run the Step 2 command.

Expected: PASS with all schema, bound, retry, URL, and secret-safe error cases.

- [ ] **Step 9: Commit**

```bash
git add src/agent_container/github_issue.py tests/container/test_github_issue.py
git commit -m "feat: add bounded GitHub Issue transport"
```

---

### Task 3: Route Issue operations through broker transport and runtime

**Files:**
- Modify: `src/agent_container/github_broker_transport.py`
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `tests/container/test_github_broker_transport.py`
- Modify: `tests/container/test_github_broker_runtime.py`

**Interfaces:**
- Consumes: `GitHubIssueTransport.list_open()`, `GitHubIssueTransport.view(number)`, `MAX_ISSUE_RESPONSE_BYTES`, and `BrokerSession.audit(issue_number=...)`.
- Produces: `_validate_issue_payload(operation: str, payload: dict[str, object]) -> dict[str, object]`.
- Produces: `handle_issue_connection(session, connection, transport, initial_request=None) -> int`.
- Extends: `handle_broker_connection(..., issue_transport: GitHubIssueTransport | None = None) -> int`.
- Extends: `UploadPackBrokerRuntime.issue_transport: GitHubIssueTransport | None` and `create()` wiring with the shared token provider.

- [ ] **Step 1: Write failing payload, dispatch, audit, and error tests**

Use accepted and denied tables:

```python
accepted = (
    ("issue-list", {}, {"issues": []}),
    ("issue-view", {"number": 12}, issue_summary(12) | {"body": "Body"}),
)
denied = (
    ("issue-list", {"page": 2}),
    ("issue-view", {"number": True}),
    ("issue-view", {"number": 12, "repository": "other/repo"}),
    ("issue-comment", {"number": 12, "body": "x"}),
)
```

Assert denied cases never call the transport. Assert success audit has `issue_number` only for view and no content sentinel. Make the fake raise `BrokerStageError("issue-request")`; assert fixed error response, fixed audit stage, and a later connection succeeds. Cover response-stream failure.

- [ ] **Step 2: Run broker tests to verify RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_transport \
  tests.container.test_github_broker_runtime -v
```

Expected: FAIL because Issue validation, handler, dispatch, and runtime wiring do not exist.

- [ ] **Step 3: Implement Issue handler and dispatch**

Add `_ISSUE_OPERATIONS = frozenset({"issue-list", "issue-view"})`. Validate exact payload keys before transport calls. Serialize compact, sorted, UTF-8 JSON with `allow_nan=False`; reject empty or over-2-MiB output before writing `ok`.

```python
if request.operation in _ISSUE_OPERATIONS and issue_transport is not None:
    return handle_issue_connection(session, connection, issue_transport, request)
```

Add `issue_transport` after `pr_transport` in the runtime dataclass and handler call. In `create()`, instantiate `GitHubIssueTransport(policy, tokens)` with the same token provider used by Git and PR transports.

- [ ] **Step 4: Run broker tests to verify GREEN**

Run the Step 2 command.

Expected: PASS for accepted, denied, audit, error isolation, response size, and runtime construction.

- [ ] **Step 5: Run Git and PR regression tests**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_git_transport \
  tests.container.test_github_pr \
  tests.container.test_github_broker_transport \
  tests.container.test_github_broker_runtime -v
```

Expected: PASS without weakening existing assertions.

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/github_broker_transport.py \
  src/agent_container/github_broker_runtime.py \
  tests/container/test_github_broker_transport.py \
  tests/container/test_github_broker_runtime.py
git commit -m "feat: route GitHub Issue read operations"
```

---

### Task 4: Add fixed CLI commands and real Unix-socket integration

**Files:**
- Modify: `src/agent_container/github_client.py`
- Modify: `tests/container/test_github_client.py`
- Modify: `tests/integration/test_github_broker_socket.py`

**Interfaces:**
- Consumes: broker operations `issue-list` and `issue-view`, 2-MiB response bound, and existing socket/capability/project environment.
- Produces: parser routes `issue list` and `issue view NUMBER`.
- Produces: `request_github_operation(operation, payload, environment, socket_factory=...) -> dict[str, Any]`; update all internal callers and tests atomically.

- [ ] **Step 1: Write failing CLI and response-bound tests**

```python
for arguments, operation, payload in (
    (["issue", "list"], "issue-list", {}),
    (["issue", "view", "12"], "issue-view", {"number": 12}),
):
    stdout, stderr = StringIO(), StringIO()
    self.assertEqual(run(arguments, environment, stdout, stderr, requester=requester), 0)
    self.assertEqual(calls[-1][:2], (operation, payload))
    self.assertEqual(stderr.getvalue(), "")
```

Assert argparse rejects `issue create`, `issue comment`, `issue list --page 2`, repository options, zero, negative, and non-integer numbers before requester invocation. Require the requester to reject array JSON, malformed JSON, oversize, denial, and truncated streams, close resources, and omit repository/token from request frames. Feed a syntactically valid but wrong operation-specific object (for example `{"issues":"not-a-list"}` for `issue-list` or a view object missing `body`) and assert the client rejects it rather than printing it.

- [ ] **Step 2: Run client tests to verify RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_client -v
```

Expected: FAIL because `issue` commands and generalized requester do not exist.

- [ ] **Step 3: Implement fixed CLI routes and generalized requester**

Add an `issue` resource with only `list` and `view`. Branch `_request_for` on `options.resource`, returning `{}` for list and `{"number": options.number}` for view. Use the shared 2-MiB maximum; add no repository, URL, query, or header options. After JSON decoding, validate exact top-level keys and the same bounded scalar/list types for the requested operation before returning the object. This client validation is a second trust boundary against a malformed or mismatched broker response; it must not reuse a builder that computes expected output from the received object.

- [ ] **Step 4: Run client tests to verify GREEN**

Run the Step 2 command.

Expected: PASS for exact frames, bounded output, fixed stderr, cleanup, and absent adjacent operations.

- [ ] **Step 5: Write failing real-socket list/view test**

Extend the integration fake with `list_open()` and `view()`. While runtime is active, request both operations. Assert fixed fields, audit operations, `issue_number` on view only, and no sentinel/capability. After runtime exit, assert stale request failure and absent runtime artifacts.

- [ ] **Step 6: Run socket integration to verify RED**

```bash
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src \
python3 -m unittest tests.integration.test_github_broker_socket -v
```

Expected: FAIL because the integration runtime does not carry Issue transport/client calls.

- [ ] **Step 7: Complete integration fixture and verify GREEN**

Pass the fake Issue transport to `UploadPackBrokerRuntime`, make both socket calls, and update exact audit assertions without weakening Git/PR checks. Rerun Step 6.

Expected: PASS including cleanup and stale-capability denial.

- [ ] **Step 8: Commit**

```bash
git add src/agent_container/github_client.py \
  tests/container/test_github_client.py \
  tests/integration/test_github_broker_socket.py
git commit -m "feat: expose GitHub Issue read commands"
```

---

### Task 5: Document operation boundaries and host smoke gate

**Files:**
- Modify: `README.md`
- Modify: `docs/phase3-github-broker.md`
- Modify: `docs/phase3-github-broker-smoke-test.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: final CLI, schema, permission, audit, and failure behavior from Tasks 1-4.
- Produces: operator instructions and not-yet-run read-only real-host checklist; no host observation is marked PASS.

- [ ] **Step 1: Write failing documentation contract tests**

```python
for expected in (
    "agent-github issue list",
    "agent-github issue view NUMBER",
    "Issues | read",
    "最大30件",
    "comment",
    "issue-request",
    "issue_number",
):
    self.assertIn(expected, guide)
```

Require the smoke guide to contain read-only list/view, PR exclusion, credential non-exposure, expired capability, existing Git/PR regression, and `not run` observation rows.

- [ ] **Step 2: Run docs tests to verify RED**

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_docs -v
```

Expected: FAIL because Issue operations and permission documentation are absent.

- [ ] **Step 3: Update operator docs and smoke checklist**

Document commands and bounded JSON without credentials. Add `Issues | read` to the App permission table and state GitHub-side approval may be required. Add `issue-request` and explain that audit omits Issue content.

Add smoke rows with observed `not run` for App permission, list/PR exclusion, view/body, write/query/cross-repository denial, non-logging, expired capability, and existing Git/PR regression. Do not create an Issue, alter App settings, start authenticated runtime, or claim PASS.

- [ ] **Step 4: Run docs tests to verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Run full automated gate**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all unit tests PASS; real Podman remains environment-gated; Task 4 socket integration already passed with its explicit flag; whitespace check emits nothing.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/phase3-github-broker.md \
  docs/phase3-github-broker-smoke-test.md tests/container/test_docs.py
git commit -m "docs: add GitHub Issue read operations"
```

---

### Task 6: Whole-branch review, CI, and PR handoff

**Files:**
- Review only: all files changed from implementation base through `HEAD`.
- Modify only for demonstrated review findings, with a failing regression test first.

**Interfaces:**
- Consumes: Tasks 1-5 and the approved spec.
- Produces: reviewed feature branch and PR; no merge, GitHub App setting mutation, real-host smoke, Issue mutation, tag, or release.

- [ ] **Step 1: Run fresh verification**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONPATH=src \
python3 -m unittest tests.integration.test_github_broker_socket -v
git diff --check
git status --short --branch
```

Expected: all automated tests PASS, socket integration PASS, whitespace clean, no uncommitted files.

- [ ] **Step 2: Request independent whole-branch code/security review**

Give the reviewer the spec, plan, base SHA, head SHA, and exact verification. Require review of no write/search/query/pagination/generic proxy, exact repository URL, exact token permissions, response bounds, PR exclusion, secret-free errors/audit, runtime cleanup, stale capability denial, and Git/PR regression.

Expected: no unresolved Critical or Important findings before push.

- [ ] **Step 3: Address findings with TDD and rerun gates**

For each valid finding, add the smallest failing test, observe RED, implement one fix, observe GREEN, and rerun Step 1. Do not bundle unrelated cleanup.

- [ ] **Step 4: Push feature branch and create PR**

Use `feat/github-issue-read-only`. Fill the PR template with behavior, security boundaries, independent review, full test evidence, real-host gate marked not run, and explicit statements that merge, App mutation, Issue mutation, tag, and release were not performed.

- [ ] **Step 5: Wait for CI and report handoff**

Confirm Unit tests and Podman integration pass on the PR head. Report PR URL, commit range, tests/skips, review findings, and the next separately approved action: merge, then GitHub App `issues: read` configuration and read-only real-host smoke.
