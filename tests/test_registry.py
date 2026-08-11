"""Tests for the artifact type registry (SPEC §5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.models.errors import Code, RegistryError
from refract.registry import (
    INLINE_MAX_BYTES,
    ArtifactRegistry,
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
