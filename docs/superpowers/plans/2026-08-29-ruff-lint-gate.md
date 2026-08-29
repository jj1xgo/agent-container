# Ruff Lint Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one pinned Ruff lint-only command shared by Codex, Claude Code, developers, and CI, and make it pass across all Python source and tests before host recovery.

**Architecture:** Define the tooling contract first as a failing documentation test. Then add an exact Ruff pin, narrow configuration, network-free common wrapper, CI install/gate ordering, and README instructions. Fix any baseline findings explicitly without auto-fix or suppression.

**Tech Stack:** Ruff 0.16.4, Python 3.11+, POSIX shell, GitHub Actions, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-29-ruff-lint-gate-design.md`

## Global Constraints

- Pin exactly `ruff==0.16.4` in a lint-only requirement file.
- Select exactly `E4`, `E7`, `E9`, and `F`; target Python `py311`; do not enable preview or formatter configuration.
- Check exactly repository `src` and `tests` through one executable `bin/lint` entrypoint.
- `bin/lint` must not install, download, format, auto-fix, or modify files.
- CI installs with `--disable-pip-version-check --no-deps` and runs lint before every Python test step.
- Do not add a third-party lint action or Ruff to `Containerfile`/the production image.
- Do not hide findings with bulk `# noqa`, per-file ignores, or global rule suppression.
- Behavior-neutral baseline cleanup is separate from tooling/configuration changes.
- Keep broker interfaces, permissions, audit, mounts, credentials, and host partial state unchanged.

---

### Task 1: Make the lint tooling contract fail

**Files:**
- Modify: `tests/container/test_docs.py`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: repository-root `ROOT` pattern and existing documentation contract tests.
- Produces: `RuffLintToolingTest`, an executable contract consumed by Task 2.

- [ ] **Step 1: Add the failing exact tooling contract**

Add import `stat` if not already present. Add this class next to the
other repository tooling/documentation tests:

```python
class RuffLintToolingTest(unittest.TestCase):
    def test_ruff_version_and_rules_are_exact(self) -> None:
        requirement = (ROOT / "requirements-lint.txt").read_text(encoding="utf-8")
        config = (ROOT / "ruff.toml").read_text(encoding="utf-8")
        self.assertEqual(requirement, "ruff==0.16.4\n")
        self.assertIn('target-version = "py311"', config)
        self.assertIn('select = ["E4", "E7", "E9", "F"]', config)
        for forbidden in ("preview", "[format]", "ignore", "per-file-ignores"):
            self.assertNotIn(forbidden, config)

    def test_common_lint_entrypoint_is_executable_and_network_free(self) -> None:
        lint = ROOT / "bin/lint"
        metadata = lint.lstat()
        body = lint.read_text(encoding="utf-8")
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertFalse(lint.is_symlink())
        self.assertTrue(metadata.st_mode & stat.S_IXUSR)
        self.assertIn("python3 -m ruff check", body)
        self.assertIn('"$REPO_ROOT/ruff.toml"', body)
        self.assertIn('"$REPO_ROOT/src"', body)
        self.assertIn('"$REPO_ROOT/tests"', body)
        for forbidden in ("pip install", "pipx", "uvx", "curl", "wget", "--fix"):
            self.assertNotIn(forbidden, body)

    def test_ci_installs_and_runs_lint_before_python_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        install = "python3 -m pip install --disable-pip-version-check --no-deps"
        requirement = "-r requirements-lint.txt"
        lint = "bin/lint"
        first_test = "python3 -m unittest"
        for required in (install, requirement, lint, first_test):
            self.assertIn(required, workflow)
        self.assertLess(workflow.index(install), workflow.index(lint))
        self.assertLess(workflow.index(lint), workflow.index(first_test))

    def test_production_image_does_not_include_ruff_tooling(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertNotIn("requirements-lint.txt", containerfile)
        self.assertNotIn("ruff", containerfile.lower())
```

Do not inspect file ownership: tracked repository files may have different
numeric owners in CI. The contract is type/symlink/executable/content based.

- [ ] **Step 2: Run the focused contract and confirm RED**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_docs.RuffLintToolingTest -v
```

Expected: errors for missing `requirements-lint.txt`, `ruff.toml`, and
`bin/lint`, plus CI contract failure. No test passes accidentally except the
production-image absence check.

- [ ] **Step 3: Commit the RED contract**

```bash
git add tests/container/test_docs.py
git commit -m "test: define Ruff lint tooling contract"
```

### Task 2: Add the pinned common Ruff gate and make the tree clean

**Files:**
- Create: `requirements-lint.txt`
- Create: `ruff.toml`
- Create: `bin/lint`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify only if Ruff reports violations: exact files under `src/` or `tests/`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: `RuffLintToolingTest` from Task 1.
- Produces: executable `bin/lint`; pinned local/CI install contract; lint-clean `src` and `tests`.

- [ ] **Step 1: Add the exact pin and narrow configuration**

Create `requirements-lint.txt` with exactly:

```text
ruff==0.16.4
```

Create `ruff.toml` with exactly:

```toml
target-version = "py311"

[lint]
select = ["E4", "E7", "E9", "F"]
```

- [ ] **Step 2: Add the common network-free wrapper**

Create executable `bin/lint`:

```sh
#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)

exec python3 -m ruff check \
  --config "$REPO_ROOT/ruff.toml" \
  "$REPO_ROOT/src" \
  "$REPO_ROOT/tests"
```

Set mode `0755`. Do not add installation or `--fix` behavior.

- [ ] **Step 3: Add CI install and lint steps before tests**

Immediately after checkout in `unit-tests`, add:

```yaml
      - name: Install lint dependency
        run: |
          python3 -m pip install --disable-pip-version-check --no-deps \
            -r requirements-lint.txt

      - name: Run Ruff lint
        run: bin/lint
```

Do not change existing test, whitespace, permissions, concurrency, or Podman
jobs.

- [ ] **Step 4: Add concise README contributor instructions**

Near the existing test/development commands, document:

```bash
python3 -m pip install --disable-pip-version-check --no-deps \
  -r requirements-lint.txt
bin/lint
```

State that Codex, Claude Code, local developers, and CI use this same command;
it checks only, does not format, does not auto-fix, and performs no network
access during `bin/lint` itself.

- [ ] **Step 5: Run the contract and confirm GREEN**

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_docs.RuffLintToolingTest -v
```

Expected: 4 tests pass.

- [ ] **Step 6: Install the pinned tool in an isolated temporary environment**

```bash
lint_venv=$(mktemp -d /tmp/agent-container-ruff.XXXXXX)
python3 -m venv "$lint_venv"
"$lint_venv/bin/python" -m pip install \
  --disable-pip-version-check --no-deps \
  -r requirements-lint.txt
PATH="$lint_venv/bin:$PATH" bin/lint
```

If sandboxed network access fails, rerun only the `pip install` command with
approved network access. Do not change the pin or install globally.

- [ ] **Step 7: Resolve baseline violations explicitly**

If Step 6 reports violations, record the exact RED diagnostics. Fix each
finding manually without `--fix`, suppression, or behavior change. Run the
focused unit test for every touched module, then rerun `PATH="$lint_venv/bin:$PATH"
bin/lint`. Commit baseline-only code/test cleanup separately:

```bash
git add src tests
git commit -m "style: resolve initial Ruff findings"
```

Skip this commit when Ruff reports no baseline violations.

- [ ] **Step 8: Commit tooling and documentation**

```bash
git add \
  .github/workflows/ci.yml \
  README.md \
  bin/lint \
  requirements-lint.txt \
  ruff.toml
git commit -m "ci: add pinned Ruff lint gate"
```

- [ ] **Step 9: Run fresh complete verification**

```bash
PATH="$lint_venv/bin:$PATH" bin/lint
PYTHONPATH=src python3 -m unittest tests.container.test_docs -v
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
git status --short --branch
```

Record exact lint status and test/skip counts. Remove only the temporary venv
after proving it contains no project state and all verification is recorded.

- [ ] **Step 10: Review the complete lint-plan diff**

Review from the commit before Task 1 through Task 2 for exact pin/rules,
wrapper network-freedom, CI ordering, no formatter/auto-fix/suppressions,
production image non-inclusion, behavior-neutral baseline changes, and pristine
test evidence. Address findings with focused fix/re-review rounds before
returning to the repository-binding plan.
