---
name: handover
description: Use when a user asks host Codex to create a handover, 引き継ぎ, or continuation record for an agent-container-managed project.
---

# Host Handover

Create one complete, factual continuation record through the constrained host publisher. The publisher derives the destination from the current Git origin and private registered-project metadata; never add a project or destination override.

## Workflow

1. Record fresh `git status --short --branch`, the latest commit, current plan, and checks actually run. Never report an unrun check as passing.
2. Compose a one-line title and a body with exactly these seven sections, in order:

   - `## 作業の目的`
   - `## 現在地`
   - `## 決定事項と理由`
   - `## 変更したファイル・commit・PR`
   - `## 検証結果`
   - `## 未解決事項とリスク`
   - `## 次の一手`

3. Reject credential material, including authentication information（認証情報）, tokens, cookies, private keys, environment values, repository numeric IDs, and pending-request bodies. Check at least `ghp_`, `github_pat_`, `sk-`, and `BEGIN PRIVATE KEY` markers.
4. Create a uniquely named `/tmp/agent-handover-*.md` regular file owned by the current user with exact mode `0600`. Put only the seven-section body in it.
5. From the intended Git workspace, run exactly:

   ```bash
   /home/tsu/.local/libexec/agent-container/agent-handover-host publish --title "TITLE" --body-file "/tmp/agent-handover-UNIQUE.md"
   ```

   Preserve `CODEX_SESSION_ID` when it is present. This installed executable is outside agent-writable workspaces; do not substitute a checkout script or Python entry point.
6. Re-read the returned path. Require a regular non-symlink mode `0600` file with the expected project, session, title, section order, Git facts, and no credential markers. Report its path and the first next action.

If publication or verification fails, report the failure and do not call the handover complete.
