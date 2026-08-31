# Task 2 report: fixed request schema and canonical Markdown

## Implementation

Implemented immutable `FamilyIssueDraft` and `CanonicalFamilyIssue` values in
`src/agent_container/family_issue.py`. Parsing requires the exact four-field
schema, validates strict string/list/tuple types, preserves accepted Unicode
without normalization, rejects invalid UTF-8 surrogates, C0/C1 controls,
newline characters, DEL, bidi overrides, empty values, and enforces UTF-8 byte
limits and 1–20 criteria. Rendering uses only the fixed Summary, Context, and
Acceptance criteria headings and fixed bullets. Canonicalization returns the
same validated title and rendered body used by preview/POST.

Added table-driven behavior tests in `tests/container/test_family_issue.py`
covering exact rendering, immutability, Unicode bytes, Markdown metacharacters,
schema/type rejection, controls/bidi/newlines, surrogates, byte boundaries,
criteria cardinality, and invalid manually-created drafts.

## TDD evidence

RED command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v
```

Output: `ImportError ... ModuleNotFoundError: No module named
'agent_container.family_issue'` (expected missing production module).

GREEN and lint command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v && bin/lint
```

Output: `Ran 12 tests ... OK`; `All checks passed!`.

## Review fix round 2

The Markdown renderer now escapes entity-like ampersands, including named and
numeric references such as `&copy;` and `&#x202E;`, preventing entity decoding
from introducing formatting or bidi controls. Escaping is now context-aware:
active inline constructs (backslash, code, emphasis, links/images,
HTML/autolinks, and entities) are protected, and block-leading headings,
quotes, bullets, thematic breaks, and ordered-list markers are protected.
Ordinary punctuation, including terminal periods and URL periods, remains
unchanged, preserving the brief's exact canonical example output. Bidi tests
cover title, summary, context, and criteria across every prohibited code point.

Review-fix RED command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v
```

Output: 4 failures: the new entity/inline tests exposed raw references and
constructs, and the restored exact example output exposed blanket period
escaping.

Review-fix GREEN command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v && bin/lint && git diff --check
```

Output: `Ran 14 tests ... OK`; `All checks passed!`.

## Verification

- Focused Task 2 tests: PASS (12 tests).
- `bin/lint`: PASS.
- `git diff --check`: PASS.
- Full unittest discovery (`PYTHONPATH=src python3 -m unittest discover -s tests -p
  'test_*.py'`): 726 run, 13 skipped, 5 errors (708 passed) in pre-existing egress socket
  tests because this sandbox denies socket `sendall` with `PermissionError:
  [Errno 1] Operation not permitted`; no Task 2 test failed.

## Self-review and concerns

The module has no credential, filesystem, network, or external-state behavior.
The canonical renderer intentionally performs no Unicode normalization; it
escapes Markdown syntax in the rendered body while retaining each accepted
input code point. The immutable draft and canonical body are fixed and shared
by preview/POST. Duplicate JSON-key rejection belongs to the
protocol decoder because duplicate keys cannot be represented by a Python
`dict`; the parser rejects non-exact/custom mapping objects. Full-suite socket
errors are environment-limited and should be rerun in a socket-capable host.

## Review fix round 1

The renderer now backslash-escapes Markdown metacharacters in summary, context,
and every acceptance criterion (`\\`, backtick, emphasis/brackets, heading,
list, quote, punctuation, and related structural characters). Renderer-owned
headings and bullets remain unescaped and are the only Markdown structure. The
tests assert exact escaped output and verify that only the three fixed heading
lines exist. Bidi override coverage now exercises title, summary, context, and
acceptance criteria for every U+202A–U+202E and U+2066–U+2069 code point.

Review-fix RED command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v
```

Output: 1 failure in `test_keeps_markdown_metacharacters_inside_fixed_sections`
because the pre-fix renderer emitted raw `#`, `>`, `*`, backticks, and list
punctuation.

Review-fix GREEN command:

```text
PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v && bin/lint
```

Output: `Ran 12 tests ... OK`; `All checks passed!`.
