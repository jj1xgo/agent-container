import unittest

from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_issue import CONTEXT_BYTES
from agent_container.family_issue import CRITERION_BYTES
from agent_container.family_issue import MAX_CRITERIA
from agent_container.family_issue import SUMMARY_BYTES
from agent_container.family_issue import TITLE_BYTES
from agent_container.family_issue import FamilyIssueDraft
from agent_container.family_issue import canonicalize_family_issue
from agent_container.family_issue import parse_family_issue_draft
from agent_container.family_issue import render_family_issue_body


def valid_payload() -> dict[str, object]:
    return {
        "title": "Add export",
        "summary": "Users need a portable copy.",
        "context": "The current UI has no export action.",
        "acceptance_criteria": ["A JSON file downloads", "Errors are visible"],
    }


class FamilyIssueTest(unittest.TestCase):
    # Break caught: a renderer that changes the fixed headings or body layout.
    def test_renders_the_fixed_canonical_markdown(self) -> None:
        draft = parse_family_issue_draft(valid_payload())

        self.assertEqual(
            render_family_issue_body(draft),
            "## Summary\n\nUsers need a portable copy.\n\n"
            "## Context\n\nThe current UI has no export action.\n\n"
            "## Acceptance criteria\n\n- A JSON file downloads\n- Errors are visible\n",
        )

    # Break caught: preview and POST deriving different canonical values.
    def test_canonical_issue_reuses_exact_title_and_rendered_body(self) -> None:
        draft = parse_family_issue_draft(valid_payload())

        self.assertEqual(
            canonicalize_family_issue(draft),
            CanonicalFamilyIssue(
                "Add export",
                render_family_issue_body(draft),
            ),
        )

    # Break caught: accepted Unicode being normalized, replaced, or re-encoded.
    def test_preserves_accepted_unicode_bytes_exactly(self) -> None:
        payload = valid_payload()
        payload.update(
            {
                "title": "Café 🚀",
                "summary": "Combining e\u0301 and emoji stay unchanged.",
                "context": "日本語もそのまま保存される。",
                "acceptance_criteria": ["Déjà vu", "Пройдено"],
            }
        )

        draft = parse_family_issue_draft(payload)

        self.assertEqual(draft.title.encode("utf-8"), "Café 🚀".encode("utf-8"))
        self.assertEqual(
            render_family_issue_body(draft).encode("utf-8"),
            (
                "## Summary\n\nCombining e\u0301 and emoji stay unchanged.\n\n"
                "## Context\n\n日本語もそのまま保存される。\n\n"
                "## Acceptance criteria\n\n- Déjà vu\n- Пройдено\n"
            ).encode("utf-8"),
        )

    # Break caught: Markdown injection through request-controlled headings or fields.
    def test_keeps_markdown_metacharacters_inside_fixed_sections(self) -> None:
        payload = valid_payload()
        payload.update(
            {
                "title": "`*` [title]",
                "summary": "# custom heading **bold** _under_ [link](https://example.invalid) = \\slash",
                "context": "> quote + - item **still content** <tag>",
                "acceptance_criteria": ["`code` {x} | ~~strike~~", "--- ! <tag>"],
            }
        )

        draft = parse_family_issue_draft(payload)
        body = render_family_issue_body(draft)

        self.assertEqual(
            body,
            "## Summary\n\n\\# custom heading \\*\\*bold\\*\\* \\_under\\_ \\[link\\]\\(https://example.invalid\\) = \\\\slash\n\n"
            "## Context\n\n\\> quote + - item \\*\\*still content\\*\\* \\<tag\\>\n\n"
            "## Acceptance criteria\n\n- \\`code\\` {x} | \\~\\~strike\\~\\~\n"
            "- --- ! \\<tag\\>\n",
        )
        self.assertEqual(body.count("## Summary"), 1)
        self.assertEqual(body.count("## Context"), 1)
        self.assertEqual(body.count("## Acceptance criteria"), 1)
        self.assertEqual(
            [line for line in body.splitlines() if line.startswith("## ")],
            ["## Summary", "## Context", "## Acceptance criteria"],
        )

    # Break caught: entity references being interpreted into formatting or bidi controls.
    def test_escapes_entity_like_ampersands_as_literal_text(self) -> None:
        payload = valid_payload()
        payload.update(
            {
                "summary": "Copyright &copy; and hidden &#x202E; marker.",
                "context": "Decimal &#8238; and named &NotAnEntity; stay visible.",
                "acceptance_criteria": ["Show &amp; exactly"],
            }
        )

        body = render_family_issue_body(parse_family_issue_draft(payload))

        self.assertIn("Copyright \\&copy; and hidden \\&#x202E; marker.", body)
        self.assertIn("Decimal \\&#8238; and named \\&NotAnEntity; stay visible.", body)
        self.assertIn("- Show \\&amp; exactly", body)
        self.assertNotIn("&#x202E;", body.replace("\\&#x202E;", ""))

    # Break caught: padded numeric references or ordinary ampersands bypassing entity escaping.
    def test_escapes_every_ampersand_including_padded_entities(self) -> None:
        payload = valid_payload()
        payload.update(
            {
                "summary": (
                    "Padded &#x00202E; &#0008238; plus &#x202E; &#8238; &copy; "
                    "&not-an-entity; and ordinary A & B."
                ),
                "context": "A & B stays literal.",
                "acceptance_criteria": ["&#x00202E; and A & B"],
            }
        )

        body = render_family_issue_body(parse_family_issue_draft(payload))

        self.assertIn(
            "Padded \\&#x00202E; \\&#0008238; plus \\&#x202E; \\&#8238; \\&copy; "
            "\\&not-an-entity; and ordinary A \\& B.",
            body,
        )
        self.assertIn("A \\& B stays literal.", body)
        self.assertIn("- \\&#x00202E; and A \\& B", body)
        self.assertNotIn("&#x00202E;", body.replace("\\&#x00202E;", ""))

    # Break caught: inline syntax and block-leading markers becoming user-owned structure.
    def test_escapes_inline_constructs_and_contextual_block_markers(self) -> None:
        payload = valid_payload()
        payload.update(
            {
                "summary": (
                    "**bold** `code` [link](https://example.invalid) "
                    "![image](https://example.invalid/i.png) <https://example.invalid> "
                    "<em>html</em> \\slash &copy;."
                ),
                "context": "# heading",
                "acceptance_criteria": ["> quote", "- bullet", "+ bullet", "1. ordered", "1) ordered"],
            }
        )

        body = render_family_issue_body(parse_family_issue_draft(payload))

        self.assertIn(
            "\\*\\*bold\\*\\* \\`code\\` \\[link\\]\\(https://example.invalid\\) "
            "\\!\\[image\\]\\(https://example.invalid/i.png\\) "
            "\\<https://example.invalid\\> \\<em\\>html\\</em\\> \\\\slash \\&copy;.",
            body,
        )
        self.assertIn("\\# heading", body)
        self.assertIn("- \\> quote", body)
        self.assertIn("- \\- bullet", body)
        self.assertIn("- \\+ bullet", body)
        self.assertIn("- 1\\. ordered", body)
        self.assertIn("- 1\\) ordered", body)

    # Break caught: a non-string, empty, or structurally wrong required value being accepted.
    def test_rejects_wrong_types_empty_values_and_non_list_criteria(self) -> None:
        cases: list[dict[str, object]] = []
        for field in ("title", "summary", "context"):
            empty = valid_payload()
            empty[field] = ""
            cases.append(empty)
            non_string = valid_payload()
            non_string[field] = 1
            cases.append(non_string)
            non_string_bool = valid_payload()
            non_string_bool[field] = True
            cases.append(non_string_bool)

        for value in (None, "criteria", {"text": "criterion"}, True, 1):
            wrong_criteria = valid_payload()
            wrong_criteria["acceptance_criteria"] = value
            cases.append(wrong_criteria)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        for value in (None, 1, True):
            payload = valid_payload()
            payload["acceptance_criteria"] = [value]
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

    # Break caught: callers smuggling repository or raw-body controls into the schema.
    def test_rejects_missing_unknown_and_duplicate_schema_fields(self) -> None:
        for field in valid_payload():
            payload = valid_payload()
            del payload[field]
            with self.subTest(case="missing", field=field):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        for field in ("repository", "body", "labels", "url"):
            payload = valid_payload()
            payload[field] = "injected"
            with self.subTest(case="unknown", field=field):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        class DuplicateMapping(dict[str, object]):
            pass

        # A mapping with duplicate JSON keys cannot be represented by dict; the parser
        # must still require an exact ordinary object rather than accepting a custom one.
        with self.assertRaises(ValueError):
            parse_family_issue_draft(DuplicateMapping(valid_payload()))

    # Break caught: controls or bidi overrides altering display or Markdown structure.
    def test_rejects_controls_bidi_overrides_and_all_newlines(self) -> None:
        for codepoint in list(range(0x20)) + list(range(0x7F, 0xA0)):
            for field in ("title", "summary", "context"):
                payload = valid_payload()
                payload[field] = f"safe{chr(codepoint)}value"
                with self.subTest(field=field, codepoint=codepoint):
                    with self.assertRaises(ValueError):
                        parse_family_issue_draft(payload)
            payload = valid_payload()
            payload["acceptance_criteria"] = [f"safe{chr(codepoint)}value"]
            with self.subTest(field="acceptance_criteria", codepoint=codepoint):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        for codepoint in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)):
            for field in ("title", "summary", "context"):
                payload = valid_payload()
                payload[field] = f"safe{chr(codepoint)}value"
                with self.subTest(field=field, codepoint=codepoint):
                    with self.assertRaises(ValueError):
                        parse_family_issue_draft(payload)
            payload = valid_payload()
            payload["acceptance_criteria"] = [f"safe{chr(codepoint)}value"]
            with self.subTest(field="acceptance_criteria", codepoint=codepoint):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        for newline in ("\n", "\r", "\r\n"):
            payload = valid_payload()
            payload["title"] = f"line{newline}break"
            with self.subTest(newline=repr(newline)):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

    # Break caught: surrogate text being accepted despite not having valid UTF-8 bytes.
    def test_rejects_invalid_utf8_surrogates(self) -> None:
        for field in ("title", "summary", "context"):
            payload = valid_payload()
            payload[field] = "bad\ud800"
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        payload = valid_payload()
        payload["acceptance_criteria"] = ["bad\udfff"]
        with self.assertRaises(ValueError):
            parse_family_issue_draft(payload)

    # Break caught: character-count limits allowing oversized UTF-8 values.
    def test_enforces_utf8_byte_limits_at_and_over_each_boundary(self) -> None:
        fields_and_limits = (
            ("title", TITLE_BYTES),
            ("summary", SUMMARY_BYTES),
            ("context", CONTEXT_BYTES),
        )
        for field, limit in fields_and_limits:
            exact = valid_payload()
            exact[field] = "é" * (limit // 2)
            if len(exact[field].encode("utf-8")) != limit:  # type: ignore[union-attr]
                exact[field] += "a"  # type: ignore[operator]
            with self.subTest(field=field, size="exact"):
                self.assertEqual(
                    len(getattr(parse_family_issue_draft(exact), field).encode("utf-8")),
                    limit,
                )

            over = valid_payload()
            over[field] = exact[field] + "a"
            with self.subTest(field=field, size="over"):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(over)

        exact_criterion = "é" * (CRITERION_BYTES // 2)
        self.assertEqual(len(exact_criterion.encode("utf-8")), CRITERION_BYTES)
        payload = valid_payload()
        payload["acceptance_criteria"] = [exact_criterion]
        self.assertEqual(parse_family_issue_draft(payload).acceptance_criteria[0], exact_criterion)
        payload["acceptance_criteria"] = [exact_criterion + "a"]
        with self.assertRaises(ValueError):
            parse_family_issue_draft(payload)

    # Break caught: criteria cardinality being unbounded or silently truncated.
    def test_enforces_one_to_twenty_acceptance_criteria(self) -> None:
        for criteria in ([], ["x"] * (MAX_CRITERIA + 1)):
            payload = valid_payload()
            payload["acceptance_criteria"] = criteria
            with self.subTest(size=len(criteria)):
                with self.assertRaises(ValueError):
                    parse_family_issue_draft(payload)

        payload = valid_payload()
        payload["acceptance_criteria"] = [f"criterion {index}" for index in range(MAX_CRITERIA)]
        draft = parse_family_issue_draft(payload)
        self.assertEqual(len(draft.acceptance_criteria), MAX_CRITERIA)

    # Break caught: mutable caller-owned collections changing an immutable draft later.
    def test_draft_and_canonical_issue_are_immutable(self) -> None:
        payload = valid_payload()
        draft = parse_family_issue_draft(payload)
        canonical = canonicalize_family_issue(draft)

        with self.assertRaises(AttributeError):
            draft.title = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            canonical.body = "changed"  # type: ignore[misc]
        payload["acceptance_criteria"] = ["changed"]
        self.assertEqual(draft.acceptance_criteria, ("A JSON file downloads", "Errors are visible"))

    # Break caught: rendering a manually constructed invalid value without validation.
    def test_render_rejects_invalid_draft_instances(self) -> None:
        invalid = FamilyIssueDraft("", "summary", "context", ("criterion",))

        with self.assertRaises(ValueError):
            render_family_issue_body(invalid)


if __name__ == "__main__":
    unittest.main()
