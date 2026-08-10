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


class MinLengthRule(BaseModel):
    """Content rule: minimum character count (SPEC §5)."""

    model_config = ConfigDict(extra="forbid")
    rule: Literal["min_length"]
    value: int = Field(ge=0)


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
    Union[RegexRule, MinLengthRule, CitationClosureRule],
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
