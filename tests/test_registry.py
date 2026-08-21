"""Tests for the artifact type registry (SPEC §5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.models.errors import Code, RegistryError
from refract.models.types import MinMatchesRule
from refract.registry import (
    INLINE_MAX_BYTES,
    ArtifactRegistry,
    apply_rules,
    check_edge,
    make_collection,
    model_slug,
    parse_type_ref,
    slugify,
    unique_slug,
)


def _write_registry(
    library_path: Path,
    types_yaml: str,
    schemas: dict[str, dict] | None = None,
) -> None:
    types_dir = library_path / "types"
    types_dir.mkdir(parents=True, exist_ok=True)
    (types_dir / "artifact_types.yaml").write_text(types_yaml, encoding="utf-8")
    if schemas:
        schema_dir = types_dir / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        for name, content in schemas.items():
            (schema_dir / name).write_text(json.dumps(content), encoding="utf-8")


# --- rules: regex / min_length ---------------------------------------------


class TestRules:
    def test_regex_rule_passes(self, tmp_path: Path) -> None:
        # SPEC §5
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
      - { rule: regex, pattern: "FR-\\\\d+" }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("requirements@v1")
        assert t is not None
        text = "intro\n# Requirements:\nsome stuff FR-12 done\n"
        assert t.check_rules(text) == []

    def test_regex_rule_fails(self, tmp_path: Path) -> None:
        # SPEC §5
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
      - { rule: regex, pattern: "FR-\\\\d+" }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("requirements@v1")
        assert t is not None
        failures = t.check_rules("no header here, no id either")
        assert len(failures) == 2
        assert all(isinstance(f, str) and f for f in failures)

    def test_regex_multiline_flag_required(self, tmp_path: Path) -> None:
        """Without the 'm' flag, ^ only matches start of string (SPEC §5)."""
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  no_flag@v1: { kind: file, format: markdown, rules: [{ rule: regex, pattern: "^header" }] }
  with_flag@v1: { kind: file, format: markdown, rules: [{ rule: regex, pattern: "^header", flags: "m" }] }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        text = "intro\nheader\n"
        no_flag = registry.get("no_flag@v1")
        with_flag = registry.get("with_flag@v1")
        assert no_flag is not None and with_flag is not None
        assert no_flag.check_rules(text) != []
        assert with_flag.check_rules(text) == []

    def test_min_length_passes(self, tmp_path: Path) -> None:
        # SPEC §5
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  design_doc@v1: { kind: file, format: markdown, rules: [{ rule: min_length, value: 10 }] }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("design_doc@v1")
        assert t is not None
        assert t.check_rules("x" * 10) == []

    def test_min_length_fails(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  design_doc@v1: { kind: file, format: markdown, rules: [{ rule: min_length, value: 2000 }] }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("design_doc@v1")
        assert t is not None
        failures = t.check_rules("too short")
        assert len(failures) == 1


# --- rules: citation_closure -------------------------------------------------


def _doc(body: str, listing: str) -> str:
    return f"{body}\n\n## СПИСОК ВИКОРИСТАНИХ ДЖЕРЕЛ\n\n{listing}\n"


ENTRY_A = "Європол. European Union Terrorism Situation and Trend Report 2025. URL: ..."
ENTRY_B = "Конвенція Ради Європи про запобігання тероризму (CETS № 196). URL: ..."


class TestCitationClosure:
    """Countable defects of a source list, settled without spending a review round."""

    def _rule(self, tmp_path: Path, min_entry_chars: int = 0) -> object:
        _write_registry(
            tmp_path,
            f"""
version: "0.1"
types:
  report@v1:
    kind: file
    format: markdown
    rules:
      - rule: citation_closure
        list_heading: СПИСОК ВИКОРИСТАНИХ ДЖЕРЕЛ
        min_entry_chars: {min_entry_chars}
""",
        )
        t = ArtifactRegistry.load(tmp_path).get("report@v1")
        assert t is not None
        return t

    def test_closed_apparatus_passes(self, tmp_path: Path) -> None:
        t = self._rule(tmp_path)
        doc = _doc(
            "Як зазначає звіт [1, с. 12], а також конвенція [2].",
            f"1. {ENTRY_A}\n2. {ENTRY_B}",
        )
        assert t.check_rules(doc) == []  # type: ignore[attr-defined]

    def test_reference_to_a_source_that_is_not_listed_fails(
        self, tmp_path: Path
    ) -> None:
        """The fabricated-citation case: the prose cites [3], the list has two entries."""
        t = self._rule(tmp_path)
        doc = _doc("Дані наведено у звіті [3].", f"1. {ENTRY_A}\n2. {ENTRY_B}")
        failures = t.check_rules(doc)  # type: ignore[attr-defined]
        assert any("[3]" in f or "3]" in f for f in failures)

    def test_entry_nobody_cites_fails(self, tmp_path: Path) -> None:
        t = self._rule(tmp_path)
        doc = _doc("Лише перше джерело [1].", f"1. {ENTRY_A}\n2. {ENTRY_B}")
        assert any("never cited" in f for f in t.check_rules(doc))  # type: ignore[attr-defined]

    def test_numbering_gap_fails(self, tmp_path: Path) -> None:
        t = self._rule(tmp_path)
        doc = _doc("Джерела [1] та [3].", f"1. {ENTRY_A}\n3. {ENTRY_B}")
        assert any("without gaps" in f for f in t.check_rules(doc))  # type: ignore[attr-defined]

    def test_stub_entry_fails_when_a_floor_is_set(self, tmp_path: Path) -> None:
        """A live run shipped `Kurt v. Turkey (1998).` as a bibliographic entry."""
        t = self._rule(tmp_path, min_entry_chars=40)
        doc = _doc("Справа [1] і звіт [2].", f"1. Kurt v. Turkey (1998).\n2. {ENTRY_A}")
        assert any("too short" in f for f in t.check_rules(doc))  # type: ignore[attr-defined]

    def test_missing_list_fails(self, tmp_path: Path) -> None:
        t = self._rule(tmp_path)
        assert t.check_rules("Текст без списку джерел [1].")  # type: ignore[attr-defined]

    def test_markdown_links_are_not_read_as_citations(self, tmp_path: Path) -> None:
        t = self._rule(tmp_path)
        doc = _doc(
            "Див. [офіційний портал](https://example.org) і звіт [1], конвенцію [2].",
            f"1. {ENTRY_A}\n2. {ENTRY_B}",
        )
        assert t.check_rules(doc) == []  # type: ignore[attr-defined]


# --- inline flag / should_inline --------------------------------------------


class TestInline:
    def test_default_inline_is_false(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  plain@v1: { kind: file, format: text }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("plain@v1")
        assert t is not None
        assert t.inline is False
        assert t.should_inline(10) is False

    def test_inline_true_under_limit(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  small@v1: { kind: file, format: text, inline: true }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("small@v1")
        assert t is not None
        assert t.should_inline(INLINE_MAX_BYTES - 1) is True

    def test_inline_boundary_at_4kb_is_exclusive(self, tmp_path: Path) -> None:
        """SPEC §5: inline permitted for size < 4KB — boundary itself is excluded."""
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  small@v1: { kind: file, format: text, inline: true }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("small@v1")
        assert t is not None
        assert t.should_inline(INLINE_MAX_BYTES) is False
        assert t.should_inline(INLINE_MAX_BYTES + 1) is False

    def test_inline_flag_false_even_under_limit(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  no_inline@v1: { kind: file, format: text, inline: false }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("no_inline@v1")
        assert t is not None
        assert t.should_inline(1) is False


# --- E_RESERVED_TYPE ---------------------------------------------------------


class TestReservedType:
    def test_declaring_reserved_name_raises(self, tmp_path: Path) -> None:
        # SPEC §5
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  verdict@v1: { kind: file, format: json }
""",
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_RESERVED_TYPE

    @pytest.mark.parametrize(
        "reserved_name", ["verdict@v1", "selection@v1", "question@v1", "answer@v1"]
    )
    def test_each_reserved_name_raises(
        self, tmp_path: Path, reserved_name: str
    ) -> None:
        _write_registry(
            tmp_path,
            f"""
version: "0.1"
types:
  {reserved_name}: {{ kind: any }}
""",
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_RESERVED_TYPE


# --- unknown type ------------------------------------------------------------


class TestUnknownType:
    def test_get_unknown_returns_none(self, tmp_path: Path) -> None:
        registry = ArtifactRegistry.builtins_only()
        assert registry.get("nope@v1") is None

    def test_knows_ref_false_for_unknown(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        assert registry.knows_ref("nope@v1") is False

    def test_knows_ref_true_for_builtin(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        assert registry.knows_ref("verdict@v1") is True

    def test_knows_ref_unwraps_collection(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        assert registry.knows_ref("collection<verdict@v1>") is True
        assert registry.knows_ref("collection<nope@v1>") is False


# --- builtin control types ---------------------------------------------------


class TestBuiltinControlTypes:
    @pytest.mark.parametrize(
        "name", ["verdict@v1", "selection@v1", "question@v1", "answer@v1"]
    )
    def test_builtin_control_types_exist_and_shape(self, name: str) -> None:
        # SPEC §5
        registry = ArtifactRegistry.builtins_only()
        t = registry.get(name)
        assert t is not None
        assert t.is_control_type is True
        assert t.kind.value == "file"
        assert t.format.value == "json"
        assert t.inline is True

    def test_non_control_type_is_not_control(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  source@v1: { kind: any }
""",
        )
        registry = ArtifactRegistry.load(tmp_path)
        t = registry.get("source@v1")
        assert t is not None
        assert t.is_control_type is False

    def test_verdict_schema_valid_payload(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("verdict@v1")
        assert t is not None
        assert t.validate_json({"verdict": "approved"}) == []
        assert t.validate_json({"verdict": "revise", "issues": [{"note": "x"}]}) == []

    def test_verdict_schema_invalid_payload(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("verdict@v1")
        assert t is not None
        assert t.validate_json({"verdict": "maybe"}) != []
        assert t.validate_json({}) != []

    def test_schema_errors_say_where_they_failed(self) -> None:
        """These messages ARE the gate-retry feedback (SPEC §10.2).

        A live retry was handed five identical `None is not of type 'string'` lines over
        a note with a dozen nested lists; the only way to act on that is to re-read the
        schema and guess, and the first attempt was paid for regardless.
        """
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("verdict@v1")
        assert t is not None
        problems = t.validate_json({"verdict": "revise", "issues": [{"note": 42}]})
        assert problems == ["issues/0/note: 42 is not of type 'string'"]

    def test_a_top_level_error_needs_no_path(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("verdict@v1")
        assert t is not None
        problems = t.validate_json({})
        assert problems and not problems[0].startswith(":")

    def test_selection_schema(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("selection@v1")
        assert t is not None
        assert t.validate_json({"winner": "rfp-doc"}) == []
        assert t.validate_json({"rationale": "no winner key"}) != []

    def test_question_schema(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("question@v1")
        assert t is not None
        assert t.validate_json({"question": "why?"}) == []
        assert t.validate_json({"context": "no question key"}) != []

    def test_answer_schema(self) -> None:
        registry = ArtifactRegistry.builtins_only()
        t = registry.get("answer@v1")
        assert t is not None
        assert t.validate_json({"answer": "42"}) == []
        assert t.validate_json({}) != []


# --- edge compatibility -------------------------------------------------------


class TestCheckEdge:
    def test_exact_match_ok(self) -> None:
        assert check_edge("extract@v1", "extract@v1", via_map=False) is None

    def test_mismatch(self) -> None:
        assert (
            check_edge("extract@v1", "source@v1", via_map=False) == Code.E_TYPE_MISMATCH
        )

    def test_collection_to_collection_ok(self) -> None:
        assert (
            check_edge(
                "collection<extract@v1>", "collection<extract@v1>", via_map=False
            )
            is None
        )

    def test_collection_to_scalar_requires_via_map(self) -> None:
        assert (
            check_edge("collection<extract@v1>", "extract@v1", via_map=False)
            == Code.E_TYPE_MISMATCH
        )
        assert check_edge("collection<extract@v1>", "extract@v1", via_map=True) is None

    def test_collection_to_mismatched_scalar(self) -> None:
        assert (
            check_edge("collection<extract@v1>", "source@v1", via_map=True)
            == Code.E_TYPE_MISMATCH
        )

    def test_scalar_to_collection_mismatch(self) -> None:
        assert (
            check_edge("extract@v1", "collection<extract@v1>", via_map=False)
            == Code.E_TYPE_MISMATCH
        )
        assert (
            check_edge("extract@v1", "collection<extract@v1>", via_map=True)
            == Code.E_TYPE_MISMATCH
        )


# --- slugify / model_slug / unique_slug / make_collection / parse_type_ref --


class TestSlugHelpers:
    def test_slugify_lowercases_and_replaces(self) -> None:
        assert slugify("Kimi K3!") == "kimi-k3"

    def test_slugify_trims_leading_trailing_dashes(self) -> None:
        assert slugify("  -Hello World- ") == "hello-world"

    def test_slugify_collapses_runs(self) -> None:
        assert slugify("a---b__c") == "a-b-c"

    def test_unique_slug_no_collision(self) -> None:
        assert unique_slug("rfp-doc", set()) == "rfp-doc"

    def test_unique_slug_collision_suffix_2(self) -> None:
        assert unique_slug("rfp-doc", {"rfp-doc"}) == "rfp-doc-2"

    def test_unique_slug_collision_suffix_3(self) -> None:
        assert unique_slug("rfp-doc", {"rfp-doc", "rfp-doc-2"}) == "rfp-doc-3"

    def test_model_slug(self) -> None:
        # SPEC §5 example
        assert model_slug("kimi/kimi-k3") == "kimi_kimi-k3"

    def test_model_slug_openai(self) -> None:
        assert model_slug("openai/gpt-5.6") == "openai_gpt-5-6"

    def test_make_collection(self) -> None:
        assert make_collection("extract@v1") == "collection<extract@v1>"

    def test_parse_type_ref_plain(self) -> None:
        assert parse_type_ref("extract@v1") == ("extract@v1", False)

    def test_parse_type_ref_collection(self) -> None:
        assert parse_type_ref("collection<extract@v1>") == ("extract@v1", True)


# --- loading a valid user artifact_types.yaml --------------------------------


class TestLoadUserRegistry:
    def test_user_types_resolve_alongside_builtins(self, tmp_path: Path) -> None:
        # SPEC §5 example from spec text
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  source@v1:        { kind: any }
  extract@v1:       { kind: file, format: json, schema: extract.schema.json }
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
      - { rule: regex, pattern: "FR-\\\\d+" }
  design_doc@v1:    { kind: file, format: markdown, rules: [{ rule: min_length, value: 2000 }] }
  discovery_report@v1: { kind: file, format: markdown }
""",
            schemas={"extract.schema.json": {"type": "object"}},
        )
        registry = ArtifactRegistry.load(tmp_path)

        # user types
        assert registry.get("source@v1") is not None
        extract = registry.get("extract@v1")
        assert extract is not None
        assert extract.schema == {"type": "object"}
        assert extract.is_builtin is False

        # builtins still present
        for name in ("verdict@v1", "selection@v1", "question@v1", "answer@v1"):
            t = registry.get(name)
            assert t is not None
            assert t.is_builtin is True

    def test_missing_types_file_yields_builtins_only(self, tmp_path: Path) -> None:
        registry = ArtifactRegistry.load(tmp_path)
        assert registry.get("verdict@v1") is not None
        assert registry.names() == list(ArtifactRegistry.builtins_only().names())

    def test_missing_schema_file_raises_e_schema(self, tmp_path: Path) -> None:
        _write_registry(
            tmp_path,
            """
version: "0.1"
types:
  extract@v1: { kind: file, format: json, schema: missing.schema.json }
""",
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_SCHEMA

    def test_malformed_yaml_raises_e_yaml(self, tmp_path: Path) -> None:
        types_dir = tmp_path / "types"
        types_dir.mkdir(parents=True)
        # Unbalanced bracket → YAML parse error.
        (types_dir / "artifact_types.yaml").write_text(
            'version: "0.1"\ntypes: {extract@v1: [\n', encoding="utf-8"
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_YAML

    def test_malformed_json_schema_raises_e_schema(self, tmp_path: Path) -> None:
        types_dir = tmp_path / "types"
        (types_dir / "schemas").mkdir(parents=True)
        (types_dir / "artifact_types.yaml").write_text(
            'version: "0.1"\n'
            "types:\n"
            "  extract@v1: { kind: file, format: json, schema: broken.schema.json }\n",
            encoding="utf-8",
        )
        # Syntactically broken JSON.
        (types_dir / "schemas" / "broken.schema.json").write_text(
            '{"type": "object",,}', encoding="utf-8"
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_SCHEMA

    def test_invalid_json_schema_document_raises_e_schema(self, tmp_path: Path) -> None:
        # Valid JSON, but not a valid Draft 2020-12 schema (type must be str/array).
        _write_registry(
            tmp_path,
            'version: "0.1"\n'
            "types:\n"
            "  extract@v1: { kind: file, format: json, schema: bad.schema.json }\n",
            schemas={"bad.schema.json": {"type": 123}},
        )
        with pytest.raises(RegistryError) as exc_info:
            ArtifactRegistry.load(tmp_path)
        assert exc_info.value.code == Code.E_SCHEMA


def test_slugify_truncates_so_run_paths_stay_within_the_windows_limit() -> None:
    """A live discover node crashed assembling its collection (SPEC §5, I-Windows).

    The agent had named a source file 63 characters long; nested as
    ``_out/sources/<slug>/<same-name>.md`` under a run tree, the path passed 260
    characters and ``shutil.copyfile`` raised a bare ``FileNotFoundError`` — which
    reads as "the agent produced nothing".
    """
    from refract.registry import MAX_SLUG_CHARS, slugify, unique_slug

    long_name = (
        "rada-yevropy-konventsiya-poperedzhennya-teroryzmu-ratyfikatsiya-ukrainy.md"
    )
    slug = slugify(long_name)
    assert len(slug) <= MAX_SLUG_CHARS
    assert not slug.endswith("-")  # cut on a separator, not mid-word
    assert slug.startswith("rada-yevropy-konventsiya")

    # short names are untouched, so existing slugs (and their reuse keys) do not move
    assert slugify("Report 2024.md") == "report-2024-md"

    # truncation can collide; that is the existing collision path's job
    taken = {slug}
    other = unique_slug(slugify(long_name.replace("ukrainy", "ukrainoyu")), taken)
    assert other != slug


# --- gate measurements (SPEC §10.2) -----------------------------------------


class TestMeasureRules:
    """The gate's verdict is binary; the measurements say HOW it passed."""

    def test_min_length_records_the_floor_and_the_actual(self) -> None:
        from refract.models.types import MinLengthRule
        from refract.registry import measure_rules

        text = "x" * 250
        measures = measure_rules([MinLengthRule(rule="min_length", value=200)], text)
        assert measures["chars"] == 250
        assert measures["min_length"] == 200

    def test_regex_recorded_per_pattern_pass_or_fail(self) -> None:
        from refract.models.types import RegexRule
        from refract.registry import measure_rules

        rules = [
            RegexRule(rule="regex", pattern=r"^## Risks", flags="m"),
            RegexRule(rule="regex", pattern=r"^## Missing", flags="m"),
        ]
        measures = measure_rules(rules, "## Risks\nsomething\n")
        assert measures["regex"] == {r"^## Risks": True, r"^## Missing": False}

    def test_citations_counted(self) -> None:
        from refract.models.types import CitationClosureRule
        from refract.registry import measure_rules

        text = (
            "Body cites [1] and [2].\n\n"
            "## REFERENCES\n"
            "1. A source entry long enough to identify the work.\n"
            "2. Short one.\n"
        )
        rule = CitationClosureRule(
            rule="citation_closure", list_heading="REFERENCES", min_entry_chars=40
        )
        measures = measure_rules([rule], text)
        citations = measures["citations"]
        assert isinstance(citations, dict)
        assert citations["entries"] == 2
        assert citations["cited"] == 2
        assert citations["shortest_entry"] == len("Short one.")
        assert citations["min_entry_chars"] == 40

    def test_measuring_never_decides_the_verdict(self) -> None:
        """A text that FAILS is still measured — that is the point (SPEC §10.2)."""
        from refract.models.types import MinLengthRule
        from refract.registry import apply_rules, measure_rules

        rule = MinLengthRule(rule="min_length", value=1000)
        assert apply_rules([rule], "short") != []
        assert measure_rules([rule], "short") == {"chars": 5, "min_length": 1000}

    def test_no_source_list_yields_no_citation_measures(self) -> None:
        from refract.models.types import CitationClosureRule
        from refract.registry import measure_rules

        rule = CitationClosureRule(rule="citation_closure", list_heading="REFERENCES")
        measures = measure_rules([rule], "prose with no list at all")
        assert "citations" not in measures


# --- forbid_regex: mechanical style defects are a gate, not a request -------


class TestForbidRegex:
    """Everything countable should cost zero tokens (SPEC §5)."""

    def _rule(self, pattern: str, **kw: object):
        from refract.models.types import ForbidRegexRule

        return ForbidRegexRule(rule="forbid_regex", pattern=pattern, **kw)

    def test_absent_pattern_passes(self) -> None:
        from refract.registry import apply_rules

        rule = self._rule(r"[^\n-]\s-\s[^\n-]")  # дефис в роли тире
        assert apply_rules([rule], "Внимание — это взвешенная сумма.") == []

    def test_present_pattern_fails_and_counts(self) -> None:
        from refract.registry import apply_rules

        rule = self._rule(r"[^\n-]\s-\s[^\n-]")
        text = "Q - это запрос, K - ключ, V - значение."
        problems = apply_rules([rule], text)
        assert len(problems) == 1
        assert "3 time(s)" in problems[0]

    def test_max_hits_tolerates_a_deliberate_exception_zone(self) -> None:
        """An ironic aside on «ты» in an article written as «вы» is not a defect."""
        from refract.registry import apply_rules

        rule = self._rule(r"\bтебе\b", max_hits=1)
        assert apply_rules([rule], "Расскажу тебе по секрету. Далее только «вы».") == []
        assert apply_rules([rule], "тебе раз, тебе два") != []

    def test_flags_are_honoured(self) -> None:
        from refract.registry import apply_rules

        rule = self._rule(r"^давайте разберём", flags="mi")
        assert apply_rules([rule], "Текст.\nДавайте разберём пример.") != []

    def test_bad_pattern_is_rejected_at_load(self) -> None:
        from pydantic import ValidationError

        from refract.models.types import ForbidRegexRule

        with pytest.raises(ValidationError):
            ForbidRegexRule(rule="forbid_regex", pattern="[unclosed")
        with pytest.raises(ValidationError):
            ForbidRegexRule(rule="forbid_regex", pattern="x", flags="q")

    def test_measured_as_a_count_even_when_passing(self) -> None:
        """`refract explain` needs the near-miss, not just the verdict (SPEC §10.2)."""
        from refract.registry import measure_rules

        rule = self._rule(r"\bтебе\b", max_hits=2)
        measures = measure_rules([rule], "тебе один раз")
        assert measures["forbidden"] == {r"\bтебе\b": 1}


# --- min_entries: a thin directory is a failure, not a pass ------------------


class TestMinEntries:
    """`kind: dir` was gated on non-emptiness alone: one figure out of four passed."""

    def _port(self, tmp_path, rules):
        from refract.artifacts import GatePort, check_port
        from refract.registry import ArtifactRegistry

        types = tmp_path / "lib" / "types"
        types.mkdir(parents=True)
        (types / "artifact_types.yaml").write_text(
            'version: "0.1"\ntypes:\n  illustration@v1: { kind: dir }\n',
            encoding="utf-8",
        )
        registry = ArtifactRegistry.load(tmp_path / "lib")
        rtype = registry.get("illustration@v1")
        assert rtype is not None
        out = tmp_path / "out"
        (out / "illustration").mkdir(parents=True)
        return (
            out,
            GatePort(port="illustration", rtype=rtype, extra_rules=tuple(rules)),
            check_port,
        )

    def _rule(self, value: int):
        from refract.models.types import MinEntriesRule

        return MinEntriesRule(rule="min_entries", value=value)

    def test_short_directory_fails_with_the_count(self, tmp_path) -> None:
        out, port, check = self._port(tmp_path, [self._rule(5)])
        for name in ("a.png", "manifest.json"):
            (out / "illustration" / name).write_text("x", encoding="utf-8")
        result = check(out, port)
        assert not result.ok
        assert "min_entries 5 not met (got 2)" in result.problems[0]
        # the near-miss is measured either way, for `refract explain`
        assert result.measures == {"entries": 2, "min_entries": 5}

    def test_enough_entries_passes(self, tmp_path) -> None:
        out, port, check = self._port(tmp_path, [self._rule(3)])
        for name in ("a.png", "b.png", "manifest.json"):
            (out / "illustration" / name).write_text("x", encoding="utf-8")
        result = check(out, port)
        assert result.ok, result.problems
        assert result.measures["entries"] == 3

    def test_dot_entries_do_not_count(self, tmp_path) -> None:
        """As everywhere else in the engine: a `.keep` is not content."""
        out, port, check = self._port(tmp_path, [self._rule(2)])
        (out / "illustration" / "a.png").write_text("x", encoding="utf-8")
        (out / "illustration" / ".keep").write_text("", encoding="utf-8")
        result = check(out, port)
        assert not result.ok
        assert "got 1" in result.problems[0]

    def test_on_a_file_type_it_says_so_instead_of_passing(self) -> None:
        """Misuse must be loud: silence would read as a satisfied rule."""
        from refract.registry import apply_rules

        problems = apply_rules([self._rule(2)], "some text")
        assert problems and "kind=dir" in problems[0]


# --- max_length: the ceiling belongs to the gate, not to a review round -----


class TestMaxLength:
    """A live run's critic spent a remark on length in all three of its rounds."""

    def _rule(self, value: int):
        from refract.models.types import MaxLengthRule

        return MaxLengthRule(rule="max_length", value=value)

    def test_within_the_ceiling_passes(self) -> None:
        from refract.registry import apply_rules

        assert apply_rules([self._rule(100)], "x" * 100) == []

    def test_over_the_ceiling_fails_with_both_numbers(self) -> None:
        from refract.registry import apply_rules

        problems = apply_rules([self._rule(100)], "x" * 137)
        assert problems == ["max_length 100 exceeded (got 137)"]

    def test_measured_even_when_passing(self) -> None:
        """`refract explain` needs "13 900 against a 14 000 ceiling", not just ok."""
        from refract.registry import measure_rules

        measures = measure_rules([self._rule(14000)], "x" * 13900)
        assert measures["chars"] == 13900
        assert measures["max_length"] == 14000

    def test_pairs_with_min_length(self) -> None:
        """A floor and a ceiling on one artifact: the genre and the assignment."""
        from refract.models.types import MinLengthRule
        from refract.registry import apply_rules

        rules = [MinLengthRule(rule="min_length", value=10), self._rule(20)]
        assert apply_rules(rules, "x" * 15) == []
        assert len(apply_rules(rules, "x" * 5)) == 1
        assert len(apply_rules(rules, "x" * 25)) == 1


# --- prose_chars: what the brief means by length is the text a reader reads ---


class TestProseChars:
    """The gate counts prose; a critic asked by eye named three different numbers."""

    def _rule(self, **kw: object):
        from refract.models.types import ProseCharsRule

        return ProseCharsRule(rule="prose_chars", **kw)

    def test_one_bound_is_required(self) -> None:
        from pydantic import ValidationError

        from refract.models.types import ProseCharsRule

        with pytest.raises(ValidationError):
            ProseCharsRule(rule="prose_chars")

    def test_min_above_max_is_rejected(self) -> None:
        from pydantic import ValidationError

        from refract.models.types import ProseCharsRule

        with pytest.raises(ValidationError):
            ProseCharsRule(rule="prose_chars", min=900, max=100)

    def test_code_blocks_do_not_count(self) -> None:
        from refract.registry import prose_chars

        prose = "Внимание — взвешенная сумма."
        with_code = prose + "\n\n```python\n" + "x = 1\n" * 200 + "```\n"
        assert prose_chars(with_code) == prose_chars(prose)

    def test_tables_images_and_urls_do_not_count(self) -> None:
        from refract.registry import prose_chars

        prose = "Строка прозы."
        noise = (
            prose
            + "\n\n| столбец | столбец |\n|---|---|\n| a | b |\n"
            + "\n![подпись к рисунку](figures/very-long-name.png)\n"
            + "\nСсылка на [текст](https://example.com/a/very/long/url) внутри.\n"
        )
        # the link TEXT is prose, its URL is not
        assert prose_chars(noise) == prose_chars(prose + "\nСсылка на текст внутри.")

    def test_whitespace_does_not_move_the_number(self) -> None:
        """A budget the writer is asked to hit must mean the same thing twice."""
        from refract.registry import prose_chars

        assert prose_chars("а  б\n\n\nв") == prose_chars("а б в")

    def test_unclosed_fence_does_not_swallow_the_article(self) -> None:
        """A draft with one stray fence still has a measurable length."""
        from refract.registry import prose_chars

        assert prose_chars("```\nx = 1\nостальная статья") > 0

    def test_over_the_ceiling_states_the_arithmetic(self) -> None:
        """The engine knows the ceiling and the count, so it says how much to remove."""
        from refract.registry import apply_rules

        problems = apply_rules([self._rule(max=100)], "я" * 137)
        assert len(problems) == 1
        assert "prose_chars max 100 exceeded (got 137)" in problems[0]
        assert "remove at least 37 characters" in problems[0]

    def test_under_the_floor_states_the_arithmetic(self) -> None:
        from refract.registry import apply_rules

        problems = apply_rules([self._rule(min=100)], "я" * 60)
        assert "add at least 40 characters" in problems[0]

    def test_measured_when_passing(self) -> None:
        from refract.registry import measure_rules

        measures = measure_rules([self._rule(min=10, max=100)], "я" * 50)
        assert measures["prose_chars"] == 50
        assert measures["prose_min"] == 10
        assert measures["prose_max"] == 100

    def test_a_file_that_passes_max_length_can_still_fail_prose(self) -> None:
        """The reason this rule exists: the file and the prose are different sizes."""
        from refract.models.types import MaxLengthRule
        from refract.registry import apply_rules

        article = "я" * 120 + "\n\n```\n" + "код\n" * 100 + "```\n"
        assert (
            apply_rules([MaxLengthRule(rule="max_length", value=1000)], article) == []
        )
        assert apply_rules([self._rule(max=100)], article) != []


# --- forbid_file: the ban list is data a person edits, not code --------------


class TestForbidFile:
    """A gate with no patterns must not look like a gate that passed (SPEC §5)."""

    def _rule(self, path: str, **kw: object):
        from refract.models.types import ForbidFileRule

        return ForbidFileRule(rule="forbid_file", path=path, **kw)

    def _list(self, tmp_path: Path, body: str) -> Path:
        f = tmp_path / "slop.txt"
        f.write_text(body, encoding="utf-8")
        return f

    def test_pattern_from_the_file_is_enforced(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "# комментарий\nстоит отметить\n\nважно понимать\n")
        problems = apply_rules(
            [self._rule("slop.txt")], "Стоит отметить, что всё работает.", tmp_path
        )
        assert len(problems) == 1
        assert "стоит отметить" in problems[0]
        assert "slop.txt" in problems[0]

    def test_clean_text_passes(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "стоит отметить\n")
        assert apply_rules([self._rule("slop.txt")], "Всё работает.", tmp_path) == []

    def test_missing_file_is_a_failure_not_silence(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        problems = apply_rules([self._rule("absent.txt")], "любой текст", tmp_path)
        assert len(problems) == 1
        assert "not found" in problems[0]

    def test_empty_file_is_a_failure(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "# только комментарии\n\n")
        problems = apply_rules([self._rule("slop.txt")], "любой текст", tmp_path)
        assert "holds no patterns" in problems[0]

    def test_broken_regex_names_its_line(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "хорошая\n[незакрытая\n")
        problems = apply_rules([self._rule("slop.txt")], "любой текст", tmp_path)
        assert any("line 2" in p for p in problems)

    def test_max_hits_allows_a_deliberate_zone(self, tmp_path: Path) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "сбой\n")
        text = "сбой первый и сбой второй"
        assert apply_rules([self._rule("slop.txt", max_hits=2)], text, tmp_path) == []
        assert apply_rules([self._rule("slop.txt", max_hits=1)], text, tmp_path) != []

    def test_measures_record_how_many_patterns_the_list_carried(
        self, tmp_path: Path
    ) -> None:
        """A run whose gate had nothing to look for has to be distinguishable."""
        from refract.registry import measure_rules

        self._list(tmp_path, "один\nдва\nтри\n")
        measures = measure_rules([self._rule("slop.txt")], "чистый текст", tmp_path)
        assert measures["forbid_file:slop.txt"] == 3

    def test_code_is_not_the_authors_writing(self, tmp_path: Path) -> None:
        """Measured on a real article: raw matching gave two false positives of three.

        `query.size(-1) ** -0.5` inside inline code read as a bold span, and a
        subtraction read as a hyphen standing in for a dash. Both would have failed a
        writer's gate over python it was right to include.
        """
        from refract.registry import apply_rules

        self._list(tmp_path, r"\*\*[^*\n]{1,200}\*\*" + "\n" + r"\S - \S" + "\n")
        article = (
            "Масштаб ищите в форме тензора: `query.size(-1) ** -0.5` в HuggingFace.\n\n"
            "```python\nscores = q @ k.transpose(-2, -1) / math.sqrt(d_k)\n"
            "delta = a - b\n```\n"
        )
        assert apply_rules([self._rule("slop.txt")], article, tmp_path) == []

    def test_the_same_pattern_in_prose_still_bites(self, tmp_path: Path) -> None:
        """The exemption is for code, not for the rule."""
        from refract.registry import apply_rules

        self._list(tmp_path, r"\*\*[^*\n]{1,200}\*\*" + "\n")
        assert (
            apply_rules([self._rule("slop.txt")], "Это **важно** понимать.", tmp_path)
            != []
        )

    def test_a_dead_phrase_inside_a_code_comment_is_not_a_finding(
        self, tmp_path: Path
    ) -> None:
        from refract.registry import apply_rules

        self._list(tmp_path, "стоит отметить\n")
        article = "Чистая проза.\n\n```python\n# стоит отметить: тут хак\nx = 1\n```\n"
        assert apply_rules([self._rule("slop.txt")], article, tmp_path) == []

    def test_the_shipped_russian_list_loads(self) -> None:
        """The library's own list is data, so it is checked like data."""
        from refract.registry import load_forbid_patterns

        library = Path(__file__).resolve().parent.parent / "library"
        patterns, problems = load_forbid_patterns(
            Path("style/forbid/ru-slop.txt"), library
        )
        assert problems == []
        assert len(patterns) > 20


# --- no_empty_sections: a heading is a promise -------------------------------


class TestNoEmptySections:
    """A floor on length passes an artifact that is only its own table of contents."""

    def _rule(self, **kw: object):
        from refract.models.types import NoEmptySectionsRule

        return NoEmptySectionsRule(rule="no_empty_sections", **kw)

    def test_filled_sections_pass(self) -> None:
        from refract.registry import apply_rules

        text = "# Разбор\n\nтекст под заголовком\n\n## Аспект\n\nи тут текст\n"
        assert apply_rules([self._rule()], text) == []

    def test_a_hollow_heading_is_named(self) -> None:
        from refract.registry import apply_rules

        text = "# Разбор\n\nвведение\n\n## Масштабирование\n\n## Маски\n\nтекст\n"
        problems = apply_rules([self._rule()], text)
        assert len(problems) == 1
        assert "Масштабирование" in problems[0]

    def test_a_container_heading_is_not_a_defect(self) -> None:
        """`# Разбор` followed straight by `## Аспект` is structure, not emptiness."""
        from refract.registry import apply_rules

        text = "# Разбор\n\n## Аспект\n\nработа под аспектом\n"
        assert apply_rules([self._rule()], text) == []

    def test_the_last_heading_counts_too(self) -> None:
        from refract.registry import apply_rules

        text = "# Разбор\n\nвведение\n\n## Выводы\n"
        problems = apply_rules([self._rule()], text)
        assert "Выводы" in problems[0]

    def test_a_hash_inside_code_is_not_a_heading(self) -> None:
        from refract.registry import apply_rules

        text = "# Разбор\n\nвведение\n\n```python\n# это комментарий\nx = 1\n```\n"
        assert apply_rules([self._rule()], text) == []

    def test_the_shape_of_a_live_failure(self) -> None:
        """68 KB of analysis with three of six aspects hollow cleared any floor."""
        from refract.models.types import MinLengthRule
        from refract.registry import apply_rules

        filled = "\n\n".join(f"## Аспект {i}\n\n" + "текст " * 400 for i in range(3))
        hollow = "\n\n".join(f"## Аспект {i}" for i in range(3, 6))
        text = "# Разбор\n\n" + filled + "\n\n" + hollow + "\n"
        assert apply_rules([MinLengthRule(rule="min_length", value=200)], text) == []
        problems = apply_rules([self._rule()], text)
        assert len(problems) == 1
        assert problems[0].startswith("3 heading(s)")

    def test_measured_when_passing(self) -> None:
        from refract.registry import measure_rules

        text = "# А\n\nтекст\n"
        assert measure_rules([self._rule()], text)["empty_sections"] == 0


class TestMinMatches:
    """`regex` answers "is this here at all", which is the wrong question for anything a
    document must have SEVERAL of. A live run shipped a requirements document with one
    requirement and no sources; every presence check passed."""

    def _rule(self, pattern: str, value: int) -> MinMatchesRule:
        return MinMatchesRule(rule="min_matches", pattern=pattern, value=value)

    def test_enough_matches_passes(self) -> None:
        text = "| FR-001 | a |\n| FR-002 | b |\n| FR-003 | c |\n"
        assert apply_rules([self._rule(r"^\|\s*FR-\d{3}\s*\|", 3)], text) == []

    def test_too_few_says_how_many_more(self) -> None:
        text = "| FR-001 | a |\n"
        problems = apply_rules([self._rule(r"^\|\s*FR-\d{3}\s*\|", 10)], text)
        assert len(problems) == 1
        assert "got 1" in problems[0]
        assert "add at least 9 more" in problems[0]

    def test_it_counts_lines_not_matches(self) -> None:
        """One line carrying two identifiers is one row, not two.

        Counting matches would let a single line satisfy a floor meant to require rows.
        """
        text = "| FR-001 | see FR-002 |\n"
        problems = apply_rules([self._rule(r"FR-\d{3}", 2)], text)
        assert problems and "got 1" in problems[0]

    def test_multiline_is_the_default(self) -> None:
        """The callers anchor per row with `^`, and a rule whose default made `^` mean
        start-of-document would silently count zero."""
        text = "intro\n| FR-001 | a |\n| FR-002 | b |\n"
        assert apply_rules([self._rule(r"^\|\s*FR-\d{3}\s*\|", 2)], text) == []

    def test_a_zero_floor_is_refused_at_load(self) -> None:
        """A floor of zero is satisfied by an empty document, so it is a mistake, not a
        configuration."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            MinMatchesRule(rule="min_matches", pattern="x", value=0)

    def test_the_prompt_shows_the_floor_and_the_pattern(self) -> None:
        """A floor of ten means nothing without saying ten of WHAT — and a bound the agent
        cannot see is a bound it fails blind, which cost a live run three rounds."""
        import pathlib

        from refract.prompt import _schema_summary

        registry = ArtifactRegistry.load(pathlib.Path("library"))
        text = _schema_summary(
            registry, "requirements@v1", [self._rule(r"^\|\s*FR-\d{3}\s*\|", 10)]
        )
        assert "At least 10 lines matching" in text
        assert "FR-" in text
