# Receive-pack delete denial hang report

## Scope

Local diagnosis and repair only. No host, GitHub, credential, fixture, evidence-status,
or smoke-gate operation was performed.

## Root cause

The receive-pack remote helper read its entire request with
`stdin.read(MAX_RECEIVE_PACK_REQUEST_BYTES + 1)`. A normal update carries a PACK body,
but a delete command ends at the pkt-line flush and Git keeps the helper input pipe open
while waiting for the server response. The helper therefore waited for client EOF and
never sent the completed delete command to the broker.

The observed component states follow directly:

1. The helper had already requested and emitted the receive-pack advertisement.
2. The broker waited for the request chunk stream.
3. Git waited for the denial response while retaining helper stdin.
4. Runtime termination finally broke the broker read, producing the later
   `status=error`, `stage=response-stream`, `ref=null` audit event.

Protected-main denial did not expose this path because that update included object data;
the delete request had no PACK body. The remote branch remaining at the normal-push OID is
also consistent with the broker never forwarding the delete to GitHub.

## TDD evidence

RED was established with
`RemoteHelperTest.test_delete_push_does_not_wait_for_client_eof`. Its input models Git's
open pipe after a complete delete flush and raises if the helper attempts to wait for EOF.
Before the production change it failed at `ReceivePackRemoteHelper.run` with:

```text
BlockingIOError: read would wait for client EOF
```

The minimal implementation now reads the bounded pkt-line command section first. If every
command has a zero new object ID, the request is complete at the flush and is sent to the
broker immediately. Non-delete updates retain the existing bounded PACK-body read.

The real Unix-socket regression
`test_denied_delete_finishes_without_waiting_for_client_eof` additionally verifies that:

- the helper reaches the broker without client EOF;
- fixed policy denies the deletion;
- the GitHub receive-pack RPC is not called;
- the audit result is `git-receive-pack` / `denied` with no ref or credential content.

## Verification

- Focused unit/broker tests: 38 passed, 3 expected socket skips.
- Unix-socket integration suite outside the filesystem socket sandbox: 4 passed.
- Exact pinned Ruff 0.16.4 wheel, SHA-256
  `f2d812e482f5a7e02eee26cd73d2a37ebbdf47d795ea63ba1b89110ae93e9fb3`:
  `All checks passed!`.
- Full suite: 585 passed, 7 skipped.
- `git diff --check`: clean.

The first socket-suite attempt inside the restricted sandbox failed only because local
Unix socket bind was denied; the approved local-only verification passed outside that
sandbox.

## Review round 1 remediation

The independent review found no Critical or Important issue and requested broader
mutation protection for the early-completion classifier. A table-driven RED test showed
that the first version incorrectly classified six malformed/ambiguous shapes as eligible
for early completion: duplicate refs, a missing first capability separator, a capability
section on a later command, a second NUL, mixed object formats, and a non-hex old OID.

The classifier now requires one non-empty capability section on the first command and no
later NUL, uniform SHA-1 or SHA-256 hex OID widths, a nonzero old OID, an all-zero new OID,
`refs/` names, unique refs, and an exact terminal flush. Table vectors also cover valid
single and multiple SHA-1 deletes, SHA-256 deletes, mixed delete/update requests,
trailing data, missing flush, empty sections, invalid lengths, and truncated packets.
Requests that are not proven valid all-delete sections retain the bounded PACK/EOF path;
the broker remains the policy authority.

Review-remediation verification before commit:

- Focused remote-helper/protocol/broker suite: 52 passed.
- Local Unix-socket integration suite: 4 passed.
- Ruff 0.16.4: `All checks passed!`.
- `git diff --check`: clean.
