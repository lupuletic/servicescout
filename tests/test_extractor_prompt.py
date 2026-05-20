"""Sanity tests for the extractor prompt.

These do NOT call an LLM. They check that the PROMPT_TEMPLATE renders
cleanly (no Python format-string surprises like an unescaped `{` in an
example), and that the rendered output mentions the catalog categories
an LLM should produce.

This guards against the class of bug where someone adds an example
containing curly braces (e.g. `${prop.name}`, `{{ .Values.foo }}`) and
the .format() call blows up at runtime when the crawler runs an
extraction.
"""

import unittest

import extractor


class PromptRendersTests(unittest.TestCase):
    def test_format_does_not_raise(self) -> None:
        # Render with the same arguments build_prompt uses.
        rendered = extractor.PROMPT_TEMPLATE.format(
            repo_id="acme/example",
            repo_path="/tmp/example",
            focus_instruction="",
            glossary="(no known components)",
        )
        self.assertGreater(len(rendered), 1000)
        self.assertIn("acme/example", rendered)

    def test_rendered_prompt_covers_catalog_categories(self) -> None:
        rendered = extractor.PROMPT_TEMPLATE.format(
            repo_id="acme/example",
            repo_path="/tmp/example",
            focus_instruction="",
            glossary="",
        )
        for keyword in [
            "components", "apis", "resources", "dependencies",
            "providers", "domain_attributes", "glossary",
            "evidence",
        ]:
            self.assertIn(keyword, rendered.lower(),
                          f"prompt missing reference to {keyword}")

    def test_prompt_contains_alias_hygiene_guidance(self) -> None:
        """Issue #2 — the alias-hygiene section should survive prompt edits."""
        rendered = extractor.PROMPT_TEMPLATE.format(
            repo_id="x", repo_path="/y", focus_instruction="", glossary="",
        )
        # Phrase markers (loose match — wording may evolve, but the
        # concept should remain).
        self.assertIn("alias", rendered.lower())
        self.assertIn("hygiene", rendered.lower())

    def test_prompt_has_evidence_discipline_rule(self) -> None:
        rendered = extractor.PROMPT_TEMPLATE.format(
            repo_id="x", repo_path="/y", focus_instruction="", glossary="",
        )
        # The "verbatim substring of the cited line" rule (post-Phase A).
        self.assertIn("verbatim", rendered.lower())

    def test_build_prompt_smoke(self) -> None:
        # The function the extractor invokes — exercises the full path.
        rendered = extractor.build_prompt({
            "id": "acme/example",
            "absolute_path": "/tmp/example",
        })
        self.assertGreater(len(rendered), 1000)


if __name__ == "__main__":
    unittest.main()
