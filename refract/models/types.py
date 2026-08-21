"""Artifact type registry file format and collection manifests (SPEC §5).

``artifact_types.yaml`` (``ArtifactTypesFile``), a single type definition
(``ArtifactTypeDef``) with its closed rule set, the ``collection<X>`` manifest
(``CollectionManifest``), and the per-item ``_item.json`` info (§10.1).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Regex flag chars accepted in a RegexRule (mapped to re.* in the registry).
_ALLOWED_REGEX_FLAGS = frozenset("misxau")


class TypeKind(str, Enum):
    file = "file"
    dir = "dir"
    any = "any"


class TypeFormat(str, Enum):
    json = "json"
    markdown = "markdown"
    text = "text"


class RegexRule(BaseModel):
    """Content rule: a regex that must be found (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")
    rule: Literal["regex"]
    pattern: str
    flags: str | None = None

    @model_validator(mode="after")
    def _validate_regex(self) -> "RegexRule":
        for ch in self.flags or "":
            if ch not in _ALLOWED_REGEX_FLAGS:
                raise ValueError(f"unknown regex flag: {ch!r}")
        try:
            re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"invalid regex pattern {self.pattern!r}: {e}") from e
        return self


class ForbidRegexRule(BaseModel):
    """Content rule: a regex that must NOT be found (SPEC §5).

    The mirror of ``regex``, and the rule mechanical style defects need: every
    check that can be counted should cost zero tokens rather than a review round.
    A hyphen standing in for a dash, straight quotes in Russian prose, a calque
    from a maintained list — all of them are "this must not appear", and asking a
    model nicely to avoid them is exactly the probabilistic enforcement the spec
    forbids for anything countable.

    ``max_hits`` allows a deliberate exception zone (an ironic aside addressing the
    reader as "ты" in an article written as "вы"): 0 means the pattern must be
    absent, N tolerates up to N occurrences.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["forbid_regex"]
    pattern: str
    flags: str | None = None
    max_hits: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_regex(self) -> "ForbidRegexRule":
        for ch in self.flags or "":
            if ch not in _ALLOWED_REGEX_FLAGS:
                raise ValueError(f"unknown regex flag: {ch!r}")
        try:
            re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"invalid regex pattern {self.pattern!r}: {e}") from e
        return self


class MinLengthRule(BaseModel):
    """Content rule: minimum character count (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")
    rule: Literal["min_length"]
    value: int = Field(ge=0)


class MaxLengthRule(BaseModel):
    """Content rule: maximum character count (SPEC §5).

    The other half of ``min_length``, and it earned its place: a live run's critic spent
    a remark on length in every one of its three rounds (18–20k, then 13k, then 13.65k
    against a brief asking 8–12k) and the article shipped over the ceiling anyway. Length
    is countable, so it belongs to the gate — a review round spent on a number is a round
    not spent on the explanation.

    A ceiling is per assignment, not per genre, so it normally lives in a node's
    ``gate_rules`` rather than in the type.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["max_length"]
    value: int = Field(ge=1)


class ProseCharsRule(BaseModel):
    """Content rule: bounds on the READABLE prose, not on the file (SPEC §5).

    ``min_length``/``max_length`` count the file, and for an article that carries python,
    formulas, tables and figure captions that is not the size the assignment talks about.
    A live run made the discrepancy visible twice: the ceiling in the pipeline had to be
    written as 14000 for a brief asking 8-12 thousand characters — a number picked to
    absorb the markdown rather than to state the article's length — and the critic still
    spent a remark on length in all three of its rounds, guessing 18-20k, then 13k, then
    13.5k for one and the same text. Asked by eye, a model cannot count; asked of the file,
    the count is of the wrong thing.

    Fenced code, inline code, table rows, image placeholders, front matter and link URLs
    are therefore removed before counting, and whitespace is collapsed, so the number does
    not move when a blank line is added.

    Both bounds are optional and at least one is required: an assignment that says nothing
    about length gets a measurement and no verdict, which is exactly what a ceiling nobody
    asked for would destroy.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["prose_chars"]
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _bounds(self) -> "ProseCharsRule":
        if self.min is None and self.max is None:
            raise ValueError("prose_chars needs at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"prose_chars min {self.min} exceeds max {self.max}")
        return self


class ForbidFileRule(BaseModel):
    """Content rule: none of the patterns listed in a FILE may appear (SPEC §5).

    ``forbid_regex`` with the patterns written into the pipeline makes editorial policy
    part of the pipeline: adding a dead phrase means editing the conveyor, and the same
    list ends up duplicated wherever it is needed — after the first edit it is two lists.
    The list of dead phrases is data a person maintains, one regex per line, and it is the
    same data the writer is held to and the style critic reports on.

    A missing or empty file is a FAILURE, never silence. A gate that found no violations
    because it had no patterns reads exactly like a gate that passed, and that is the one
    way a mechanical check can lie.

    Matched against the PROSE (as ``prose_chars`` counts it), not the raw file — code is
    not the author's writing. ``forbid_regex`` deliberately keeps raw-text semantics: it
    is the general mechanical rule and a type may use it on structure. Measured on a real
    article, raw matching gave two false positives out of three hits: an exponent
    ``query.size(-1) ** -0.5`` inside inline code read as a bold span, and a subtraction
    read as a hyphen standing in for a dash. Both would have failed a writer's gate over
    python it was right to include.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["forbid_file"]
    # relative to the library root, so a pipeline vendored elsewhere keeps working
    path: str
    flags: str | None = "i"
    max_hits: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_flags(self) -> "ForbidFileRule":
        for ch in self.flags or "":
            if ch not in _ALLOWED_REGEX_FLAGS:
                raise ValueError(f"unknown regex flag: {ch!r}")
        return self


class NoEmptySectionsRule(BaseModel):
    """Content rule: every heading must have work under it (SPEC §5).

    A floor on length answers "did the agent write anything" and misses what agents
    actually do with a structured artifact: they write the SHAPE first — every heading the
    contract asks for, in order — and fill it section by section over several passes. Both
    live failures cleared a floor comfortably: one analysis was 1 748 bytes of headings
    alone, another was 68 KB with three of six aspects still hollow. The stage downstream
    then builds a whole section of the article on nothing.

    A heading followed by a deeper heading is a container and is fine; a heading followed
    by nothing but a sibling or a shallower heading is empty.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["no_empty_sections"]
    # one visible character is the honest floor: this rule answers "is anything there",
    # and "is there enough" is min_length's question, asked separately
    min_chars: int = Field(default=1, ge=1)


class MinEntriesRule(BaseModel):
    """Content rule for ``kind: dir``: at least N entries must be present (SPEC §5).

    The sibling of ``min_length`` for directory artifacts. Existence and non-emptiness
    are already checked, and that is exactly the hole: a step that owed four figures and
    produced one passes a non-empty directory. The node that knows how many it asked for
    states the number (``gate_rules``), and the count stops being a thing a reader has
    to notice.

    Dot-entries do not count, as everywhere else in the engine: a directory holding only
    a ``.keep`` is not content.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["min_entries"]
    value: int = Field(ge=1)


class MinMatchesRule(BaseModel):
    """Content rule for markdown: a pattern must match at least ``value`` times (SPEC §5).

    ``regex`` answers "does the document have this at all", which is the wrong question for
    anything a document is supposed to have SEVERAL of. A requirements document with one
    requirement satisfies every ``regex`` rule about requirements; so does one with sixty.
    Measured on a live run: a document with ten requirements, no sources and a single
    section passed a gate built entirely out of presence checks.

    The count is of matching lines, not of matches, so a pattern anchored per row with
    ``(?m)^`` counts rows. That is what the callers want — table rows, subsections,
    identifiers — and it makes a repeated match inside one line count once.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["min_matches"]
    pattern: str
    value: int = Field(ge=1)
    flags: str = "m"


class CitationClosureRule(BaseModel):
    """Content rule: the document's numbered source list has to hold together.

    For a document that cites sources as ``[12]`` / ``[12, с. 45]`` against a
    numbered list under ``list_heading``. Fabricated references and orphaned
    entries are countable, so they are settled mechanically instead of costing a
    critic a review round — the same reason ``min_length`` exists.
    """

    model_config = ConfigDict(extra="forbid")
    rule: Literal["citation_closure"]
    # a regex, not a literal, so one type can serve documents in several languages:
    # "СПИСОК ВИКОРИСТАНИХ ДЖЕРЕЛ|REFERENCES|BIBLIOGRAPHY"
    list_heading: str
    # a bibliographic entry that is only a few characters long is a stub, not a
    # description someone could follow back to the source
    min_entry_chars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_heading(self) -> "CitationClosureRule":
        try:
            re.compile(self.list_heading)
        except re.error as exc:
            raise ValueError(f"invalid list_heading regex: {exc}") from exc
        return self


Rule = Annotated[
    Union[
        RegexRule,
        ForbidRegexRule,
        ForbidFileRule,
        MinLengthRule,
        MaxLengthRule,
        ProseCharsRule,
        NoEmptySectionsRule,
        MinEntriesRule,
        MinMatchesRule,
        CitationClosureRule,
    ],
    Field(discriminator="rule"),
]


class ArtifactTypeDef(BaseModel):
    """One entry of the artifact type registry (SPEC §5).

    ``kind ∈ {file, dir, any}``; ``format`` and ``schema``/``rules`` only for
    ``file``; ``schema`` only for ``json``. ``inline`` permits inlining content
    < 4 KB into prompts.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: TypeKind
    format: TypeFormat | None = None
    # 'schema' shadows a BaseModel attribute — store as schema_file, alias "schema".
    schema_file: str | None = Field(default=None, alias="schema")
    rules: list[Rule] = Field(default_factory=list)
    inline: bool = False

    @model_validator(mode="after")
    def _consistency(self) -> "ArtifactTypeDef":
        if self.kind is not TypeKind.file:
            if self.format is not None:
                raise ValueError("format is only valid for kind=file")
            if self.schema_file is not None:
                raise ValueError("schema is only valid for kind=file")
            if self.rules:
                raise ValueError("rules are only valid for kind=file")
        if self.schema_file is not None and self.format is not TypeFormat.json:
            raise ValueError("schema is only valid for format=json")
        return self


class ArtifactTypesFile(BaseModel):
    """``library/types/artifact_types.yaml`` (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")

    version: str
    types: dict[str, ArtifactTypeDef] = Field(default_factory=dict)


class CollectionStatus(str, Enum):
    ok = "ok"
    failed = "failed"


class CollectionItem(BaseModel):
    """One element of a ``collection<X>`` manifest (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: str
    source_hash: str
    status: CollectionStatus = CollectionStatus.ok
    path: str
    error: str | None = None


class CollectionStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = 0
    ok: int = 0
    failed: int = 0


class CollectionManifest(BaseModel):
    """``_collection.json`` (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")

    type: str
    items: list[CollectionItem] = Field(default_factory=list)
    stats: CollectionStats = Field(default_factory=CollectionStats)


class ItemInfo(BaseModel):
    """``_item.json`` materialized alongside a map element's payload (SPEC §10.1)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: str
    source_hash: str
