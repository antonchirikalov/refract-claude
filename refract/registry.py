"""Artifact type registry (SPEC §5).

Loads ``library/types/artifact_types.yaml`` and injects the built-in control
types (verdict@v1, selection@v1, question@v1, answer@v1). Owns slugify, the
``collection<X>`` type constructor, the rule set, and edge compatibility.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError as PydanticValidationError

from refract.models.errors import Code, RegistryError
from refract.models.types import (
    ArtifactTypeDef,
    ArtifactTypesFile,
    CitationClosureRule,
    MinLengthRule,
    RegexRule,
    Rule,
    TypeFormat,
    TypeKind,
)

# --- constants -------------------------------------------------------------

INLINE_MAX_BYTES = 4096  # SPEC §5 / §11: inline permitted only below this size

_BUILTIN_SCHEMA_DIR = Path(__file__).parent / "schemas"

# Built-in control types are NOT in the registry file; the engine injects them
# (kind file, format json, inline true) with schemas from refract/schemas/.
CONTROL_TYPES: dict[str, str] = {
    "verdict@v1": "verdict.schema.json",
    "selection@v1": "selection.schema.json",
    "question@v1": "question.schema.json",
    "answer@v1": "answer.schema.json",
}

_TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*@v\d+$")
_COLLECTION_RE = re.compile(r"^collection<(.+)>$")

_EXT: dict[TypeFormat, str] = {
    TypeFormat.json: ".json",
    TypeFormat.markdown: ".md",
    TypeFormat.text: ".txt",
}

_REGEX_FLAGS: dict[str, int] = {
    "m": re.MULTILINE,
    "i": re.IGNORECASE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
    "a": re.ASCII,
    "u": re.UNICODE,
}


# --- type-ref helpers (collection constructor + edge compatibility) --------


def parse_type_ref(ref: str) -> tuple[str, bool]:
    """Split a type reference into ``(inner_name, is_collection)`` (SPEC §5)."""
    m = _COLLECTION_RE.match(ref)
    if m:
        return m.group(1), True
    return ref, False


def make_collection(inner: str) -> str:
    """The ``collection<X>`` type constructor (SPEC §5)."""
    return f"collection<{inner}>"


def check_edge(source_type: str, target_type: str, *, via_map: bool) -> Code | None:
    """Edge compatibility (SPEC §5).

    ``T → T`` (exact). ``collection<X>`` connects to a ``collection<X>`` input,
    or to an ``X`` input only through ``map:``. Returns ``E_TYPE_MISMATCH`` on
    an incompatible edge, else ``None``.
    """
    s_inner, s_coll = parse_type_ref(source_type)
    t_inner, t_coll = parse_type_ref(target_type)

    if not s_coll and not t_coll:
        return None if source_type == target_type else Code.E_TYPE_MISMATCH
    if s_coll and t_coll:
        return None if s_inner == t_inner else Code.E_TYPE_MISMATCH
    if s_coll and not t_coll:
        if via_map and s_inner == t_inner:
            return None
        return Code.E_TYPE_MISMATCH
    # target is a collection but source is not
    return Code.E_TYPE_MISMATCH


# --- slugify (single implementation, SPEC §5) ------------------------------


# A slug becomes a directory name inside an already-deep run tree, and it is derived
# from a filename an agent chose — which in a live run ran to 63 characters and, paired
# with the file of the same name inside it, overran the Windows path limit. Capping keeps
# slugs legible as well: past this length they stop being names and become sentences.
# CHANGED (2026-07-31, SPEC §5): slugify truncates.
MAX_SLUG_CHARS = 48


def slugify(s: str) -> str:
    """lowercase; ``[^a-z0-9]+ → -``; trim leading/trailing ``-`` (SPEC §5).

    Truncated to ``MAX_SLUG_CHARS`` on a ``-`` boundary where there is one, so the
    result stays readable rather than ending mid-word. Collisions introduced by
    truncation are resolved by ``unique_slug`` exactly like any other collision.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if len(slug) <= MAX_SLUG_CHARS:
        return slug
    cut = slug[:MAX_SLUG_CHARS]
    head, sep, _ = cut.rpartition("-")
    return (head if sep and len(head) >= MAX_SLUG_CHARS // 2 else cut).strip("-")


def unique_slug(base: str, taken: set[str]) -> str:
    """Resolve slug collisions with ``-2``, ``-3`` suffixes (SPEC §5)."""
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def model_slug(model: str) -> str:
    """``slugify(provider) + "_" + slugify(model_id)`` (SPEC §5), e.g. kimi_kimi-k3."""
    provider, _, model_id = model.partition("/")
    return f"{slugify(provider)}_{slugify(model_id)}"


# --- rules -----------------------------------------------------------------


def apply_rules(rules: Sequence[Rule], text: str) -> list[str]:
    """Rule-failure messages for this text; empty means every rule passes (§5).

    A free function, not only a method of the type: a node may tighten the gate of
    its own output (``gate_rules``, SPEC §8), and those rules are checked with the
    very same code as the type's own.
    """
    failures: list[str] = []
    for rule in rules:
        if isinstance(rule, RegexRule):
            flags = 0
            for ch in rule.flags or "":
                flags |= _REGEX_FLAGS.get(ch, 0)
            if re.search(rule.pattern, text, flags) is None:
                failures.append(f"regex {rule.pattern!r} not found")
        elif isinstance(rule, MinLengthRule):
            if len(text) < rule.value:
                failures.append(f"min_length {rule.value} not met (got {len(text)})")
        elif isinstance(rule, CitationClosureRule):
            failures.extend(check_citation_closure(text, rule))
    return failures


def measure_rules(rules: Sequence[Rule], text: str) -> dict[str, object]:
    """What the rules MEASURED, pass or fail (SPEC §10.2).

    The gate's verdict is binary, and that hid a question worth asking: a report that
    cleared a 20 000-character floor at 20 100 is not the same artifact as one that
    cleared it at 80 000, and nothing in the run said which had happened. Measurements
    are recorded on success too, so `refract explain` can point at what barely passed.

    Never raises and never affects the verdict — a measurement that cannot be taken is
    simply absent from the result.
    """
    measures: dict[str, object] = {"chars": len(text)}
    regexes: dict[str, bool] = {}
    for rule in rules:
        if isinstance(rule, RegexRule):
            flags = 0
            for ch in rule.flags or "":
                flags |= _REGEX_FLAGS.get(ch, 0)
            regexes[rule.pattern] = re.search(rule.pattern, text, flags) is not None
        elif isinstance(rule, MinLengthRule):
            measures["min_length"] = rule.value
        elif isinstance(rule, CitationClosureRule):
            facts = measure_citations(text, rule)
            if facts is not None:
                measures["citations"] = facts
    if regexes:
        measures["regex"] = regexes
    return measures


# --- citation closure ------------------------------------------------------


def _cited_numbers(body: str) -> set[int]:
    """Source numbers referenced in the prose: ``[12]``, ``[12, с. 45]``, ``[3; 5]``."""
    cited: set[int] = set()
    for group in re.findall(r"\[([^\[\]]{1,120})\]", body):
        for chunk in re.split(r"[;,]", group):
            chunk = chunk.strip()
            if chunk.isdigit():
                cited.add(int(chunk))
    return cited


def _split_citations(
    text: str, rule: CitationClosureRule
) -> tuple[str, list[tuple[str, str]]] | None:
    """``(prose before the list, [(number, entry), ...])``, or None if there is no list.

    One parser for both the check and the measurement, so a document can never be
    judged against one reading of its source list and described by another.
    """
    parts = re.split(rf"^#{{1,3}}\s*(?:{rule.list_heading}).*$", text, flags=re.M)
    if len(parts) < 2:
        return None
    entries = re.findall(r"^\s*(\d+)[.)]\s+(.+?)\s*$", parts[-1], flags=re.M)
    return parts[0], entries


def measure_citations(text: str, rule: CitationClosureRule) -> dict[str, object] | None:
    """Countable facts about the reference apparatus (SPEC §10.2); None if no list."""
    parsed = _split_citations(text, rule)
    if parsed is None:
        return None
    body, entries = parsed
    facts: dict[str, object] = {
        "entries": len(entries),
        "cited": len(_cited_numbers(body)),
    }
    if entries:
        facts["shortest_entry"] = min(len(entry.strip()) for _, entry in entries)
    if rule.min_entry_chars:
        facts["min_entry_chars"] = rule.min_entry_chars
    return facts


def check_citation_closure(text: str, rule: CitationClosureRule) -> list[str]:
    """Every reference resolves, every entry is used, no gaps, no stub entries.

    A fabricated reference and an entry nobody cites are both countable defects;
    catching them here means the critic never has to spend a round on them.
    """
    parsed = _split_citations(text, rule)
    if parsed is None:
        return [f"citation_closure: no source list under {rule.list_heading!r}"]

    body, entries = parsed
    if not entries:
        return [f"citation_closure: source list under {rule.list_heading!r} is empty"]

    failures: list[str] = []
    numbers = [int(n) for n, _ in entries]
    listed = set(numbers)
    if len(numbers) != len(listed):
        dupes = sorted({n for n in numbers if numbers.count(n) > 1})
        failures.append(f"citation_closure: duplicate source numbers {dupes}")
    expected = set(range(1, len(numbers) + 1))
    if listed != expected:
        failures.append(
            "citation_closure: source list is not numbered 1..N without gaps "
            f"(got {sorted(listed)})"
        )

    cited = _cited_numbers(body)
    dangling = sorted(cited - listed)
    if dangling:
        failures.append(
            f"citation_closure: cited in the text but absent from the list: {dangling}"
        )
    unused = sorted(listed - cited)
    if unused:
        failures.append(
            f"citation_closure: listed but never cited in the text: {unused}"
        )

    if rule.min_entry_chars:
        stubs = [n for n, entry in entries if len(entry.strip()) < rule.min_entry_chars]
        if stubs:
            failures.append(
                f"citation_closure: entries too short to identify the source "
                f"(under {rule.min_entry_chars} chars): {sorted(stubs)}"
            )
    return failures


# --- resolved type ---------------------------------------------------------


class ResolvedType:
    """A registered artifact type with its (optional) compiled JSON schema."""

    def __init__(
        self,
        name: str,
        defn: ArtifactTypeDef,
        *,
        is_builtin: bool,
        schema: dict[str, object] | None,
    ) -> None:
        self.name = name
        self.kind: TypeKind = defn.kind
        self.format: TypeFormat | None = defn.format
        self.schema_file: str | None = defn.schema_file
        self.rules: list[Rule] = defn.rules
        self.inline: bool = defn.inline
        self.is_builtin = is_builtin
        self.schema = schema
        self._validator = Draft202012Validator(schema) if schema is not None else None

    @property
    def is_control_type(self) -> bool:
        return self.name in CONTROL_TYPES

    def ext(self) -> str | None:
        """Artifact file extension for this type's format (SPEC §10.4)."""
        return _EXT.get(self.format) if self.format is not None else None

    def should_inline(self, size_bytes: int) -> bool:
        """Whether content of this size may be inlined into a prompt (SPEC §5/§11)."""
        return self.inline and size_bytes < INLINE_MAX_BYTES

    def check_rules(self, text: str) -> list[str]:
        """Return a list of rule-failure messages; empty means all rules pass (§5)."""
        return apply_rules(self.rules, text)

    def validate_json(self, data: object) -> list[str]:
        """Return JSON-schema error messages; empty means valid (SPEC §10.2 gate)."""
        if self._validator is None:
            return []
        return [e.message for e in self._validator.iter_errors(data)]


# --- registry --------------------------------------------------------------


def _validate_type_name(name: str) -> None:
    if not _TYPE_NAME_RE.match(name):
        raise RegistryError(Code.E_SCHEMA, f"invalid type name: {name!r}")


def _read_json_schema(path: Path) -> dict[str, object]:
    """Read + parse a JSON schema file and validate the schema itself (SPEC §5).

    A malformed JSON document or an invalid schema surfaces as ``E_SCHEMA`` at
    load time, not as an opaque crash later during gate validation.
    """
    try:
        schema: dict[str, object] = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise RegistryError(
            Code.E_SCHEMA, f"invalid JSON schema {path.name}: {e}"
        ) from e
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise RegistryError(
            Code.E_SCHEMA, f"invalid JSON schema {path.name}: {e.message}"
        ) from e
    return schema


class ArtifactRegistry:
    """The resolved set of artifact types (user + injected built-ins)."""

    def __init__(self, types: dict[str, ResolvedType]) -> None:
        self._types = types

    @staticmethod
    def _load_builtins() -> dict[str, ResolvedType]:
        out: dict[str, ResolvedType] = {}
        for name, fname in CONTROL_TYPES.items():
            schema = _read_json_schema(_BUILTIN_SCHEMA_DIR / fname)
            defn = ArtifactTypeDef.model_validate(
                {
                    "kind": "file",
                    "format": "json",
                    "inline": True,
                    "schema": fname,
                }
            )
            out[name] = ResolvedType(name, defn, is_builtin=True, schema=schema)
        return out

    @classmethod
    def builtins_only(cls) -> "ArtifactRegistry":
        """Registry containing only the injected control types (SPEC §5)."""
        return cls(cls._load_builtins())

    @classmethod
    def load(cls, library_path: Path | str) -> "ArtifactRegistry":
        """Load ``library/types/artifact_types.yaml`` and inject built-ins (SPEC §5).

        Raises ``RegistryError`` with ``E_RESERVED_TYPE`` if a user type reuses a
        reserved control name, ``E_YAML`` if the types file is not valid YAML, or
        ``E_SCHEMA`` on a schema-invalid types file, a missing referenced JSON
        schema, or a malformed/invalid referenced JSON schema.
        """
        library_path = Path(library_path)
        types = cls._load_builtins()

        types_file = library_path / "types" / "artifact_types.yaml"
        if not types_file.exists():
            return cls(types)

        try:
            raw = yaml.safe_load(types_file.read_text("utf-8")) or {}
        except yaml.YAMLError as e:
            raise RegistryError(Code.E_YAML, f"{types_file}: {e}") from e

        try:
            parsed = ArtifactTypesFile.model_validate(raw)
        except PydanticValidationError as e:
            raise RegistryError(Code.E_SCHEMA, f"{types_file}: {e}") from e

        schema_dir = library_path / "types" / "schemas"
        for name, defn in parsed.types.items():
            if name in CONTROL_TYPES:
                raise RegistryError(
                    Code.E_RESERVED_TYPE,
                    f"{name!r} is a reserved built-in control type",
                )
            _validate_type_name(name)
            schema: dict[str, object] | None = None
            if defn.schema_file is not None:
                schema_path = schema_dir / defn.schema_file
                if not schema_path.exists():
                    raise RegistryError(
                        Code.E_SCHEMA,
                        f"schema file not found for {name}: {defn.schema_file}",
                    )
                schema = _read_json_schema(schema_path)
            types[name] = ResolvedType(name, defn, is_builtin=False, schema=schema)

        return cls(types)

    # --- lookups -----------------------------------------------------------

    def get(self, name: str) -> ResolvedType | None:
        return self._types.get(name)

    def has(self, name: str) -> bool:
        return name in self._types

    def knows_ref(self, ref: str) -> bool:
        """Whether a (possibly ``collection<X>``) reference names a known type."""
        inner, _ = parse_type_ref(ref)
        return inner in self._types

    def is_reserved(self, name: str) -> bool:
        return name in CONTROL_TYPES

    def is_control_type(self, name: str) -> bool:
        return name in CONTROL_TYPES

    def names(self) -> list[str]:
        return list(self._types)
