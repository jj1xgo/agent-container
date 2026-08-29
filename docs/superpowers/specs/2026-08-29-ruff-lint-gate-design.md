# Ruff lint gate design

Date: 2026-08-29

## Context

The repository currently uses Python `unittest` and `git diff --check` but has
no static Python lint configuration or CI lint gate. The project-scoped GitHub
repository binding work adds security-sensitive policy parsing, filesystem
migration, and failure-path code. A common early lint gate should cover that
work before host recovery and release verification.

Codex, Claude Code, host operators, and CI must use one repository-owned lint
contract. Agent-specific installations or prompts must not define different
rules.

## Goal

Add a pinned, deterministic Ruff lint-only gate for all Python source and test
code, and run it before unit tests in CI.

## Non-goals

- Do not enable Ruff formatting or add a formatting gate.
- Do not auto-fix code in CI or in the default repository command.
- Do not add Ruff to the production container image or broker runtime.
- Do not add broad preview, style, complexity, annotation, import-order, or
  security rule families in this change.
- Do not hide baseline findings with bulk `# noqa`, per-file ignores, or global
  rule suppression.
- Do not change Python behavior merely to satisfy lint.

## Toolchain

Pin Ruff exactly to `0.16.4` in `requirements-lint.txt`. It is the current
official PyPI release at design time. The lint environment installs only this
package with dependency resolution disabled:

```sh
python3 -m pip install --disable-pip-version-check --no-deps \
  -r requirements-lint.txt
```

Dependency installation is explicit and separate from lint execution. The
default lint command never downloads tools or modifies an environment.

## Configuration

Store the repository contract in `ruff.toml`:

```toml
target-version = "py311"

[lint]
select = ["E4", "E7", "E9", "F"]
```

Do not enable `preview`. Ruff's default excludes remain unchanged. The common
entrypoint explicitly checks `src` and `tests`, so unrelated state directories,
worktrees, caches, and host-private state are not traversed.

## Common entrypoint

Add executable `bin/lint` as a POSIX shell wrapper. It resolves the repository
root from its own path and executes:

```sh
python3 -m ruff check --config "$REPO_ROOT/ruff.toml" \
  "$REPO_ROOT/src" "$REPO_ROOT/tests"
```

The wrapper uses `set -eu` and does not invoke `pip`, `uv`, `pipx`, `curl`, or
another installer. If Ruff is absent or has the wrong environment setup, it
fails without network access or source modification. Ruff's normal diagnostics
and exit status are preserved.

Codex, Claude Code, developers, and CI all invoke `bin/lint`; no client-specific
lint command or configuration is introduced.

## CI integration

In the existing `unit-tests` job, add these steps after checkout and before all
unit/integration test steps:

1. Install the exact lint requirement with `--no-deps`.
2. Run `bin/lint`.

Do not use an additional third-party lint action. This keeps the executable,
version, and configuration identical to the local path and avoids another
mutable action dependency. Existing test, whitespace, and Podman jobs remain
otherwise unchanged.

## Baseline handling

Run Ruff once after adding the pin, configuration, and wrapper. If the existing
tree has findings, preserve the RED output and fix each violation explicitly.
Behavior-neutral source/test corrections are committed separately from the
tooling/configuration commit so review can distinguish infrastructure from
baseline cleanup.

Do not use `--fix` for baseline cleanup. Do not add bulk suppressions. If a
finding cannot be resolved without changing behavior or weakening a security
boundary, stop and review that finding rather than forcing the gate green.

## Executable documentation contract

Extend `tests/container/test_docs.py` with a lint-tooling contract that verifies:

- `requirements-lint.txt` contains exactly `ruff==0.16.4` plus one trailing
  newline.
- `ruff.toml` declares `py311` and exactly `E4`, `E7`, `E9`, and `F` without
  preview or formatter configuration.
- `bin/lint` is an executable regular non-symlink file and invokes
  `python3 -m ruff check` for `src` and `tests` with the repository config.
- `.github/workflows/ci.yml` installs `requirements-lint.txt` with `--no-deps`,
  invokes `bin/lint`, and places both steps before the first Python test step.
- `Containerfile` does not install or copy Ruff-specific tooling.

README contributor instructions document the explicit install command and
`bin/lint`. The instructions state that lint does not format or auto-fix.

## Verification

The implementation follows test-driven development:

1. Add the failing documentation/tooling contract and confirm RED because the
   files and CI steps are absent.
2. Add the exact pin, configuration, wrapper, CI steps, and README instructions.
3. Run `bin/lint`; if findings exist, commit reviewed behavior-neutral cleanup
   separately.
4. Run the focused contract, all documentation tests, the complete Python test
   suite, and `git diff --check`.
5. Review that production image inputs, broker interfaces, permissions, and
   credential boundaries are unchanged.

The Ruff gate must be green before repository-binding Task 5 documentation/full
review and before any host partial-state recovery.
