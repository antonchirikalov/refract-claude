"""Prompt assembly (SPEC §11).

Builds the TASK prompt (items 2–4 of §11): the inputs section, the outputs
section, and the optional revision / gate_feedback additions — all GENERATED
from the agent contract and the registry (I5), never hand-written. The agent's
``prompt.md`` (item 1) is the system prompt and is passed separately.

Input inlining reads the already-materialized ``input/<port>/`` tree, so this
runs after materialization (§10.1). Only relative paths appear (I1).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from refract.artifacts import artifact_filename
from refract.models.agent import AgentSpec, Port
from refract.models.types import (
    CitationClosureRule,
    ForbidFileRule,
    ForbidRegexRule,
    MaxLengthRule,
    MinEntriesRule,
    MinLengthRule,
    NoEmptySectionsRule,
    ProseCharsRule,
    RegexRule,
    Rule,
    TypeKind,
)
from refract.registry import ArtifactRegistry, load_forbid_patterns, parse_type_ref

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_COLLECTION_INLINE_MAX_ITEMS = 50  # SPEC §11
INLINE_ELEMENT_MAX_BYTES = 4096  # per collection element payload (SPEC §5 inline)

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


@dataclass
class RevisionContext:
    """Data for the loop revision addition (SPEC §11 item 4)."""

    previous_path: str
    verdict_json: str
    hint: str | None = None


def build_task_prompt(
    *,
    agent: AgentSpec,
    registry: ArtifactRegistry,
    workdir: Path | str,
    revision: RevisionContext | None = None,
    gate_feedback: str | None = None,
    gate_rules: Sequence[Rule] = (),
) -> str:
    """Assemble the task prompt (SPEC §11 items 2–4) for one step."""
    workdir = Path(workdir)
    sections = [
        _render_inputs(agent, registry, workdir),
        _render_outputs(agent, registry, gate_rules),
    ]
    if revision is not None:
        sections.append(
            _env.get_template("revision.md.j2").render(
                previous_path=revision.previous_path,
                verdict=revision.verdict_json,
                hint=revision.hint,
            )
        )
    if gate_feedback is not None:
        sections.append(
            _env.get_template("gate_feedback.md.j2").render(report=gate_feedback)
        )
    return "\n".join(s.strip("\n") for s in sections if s.strip()) + "\n"


# --- inputs (SPEC §11 item 2) ----------------------------------------------


def _render_inputs(agent: AgentSpec, registry: ArtifactRegistry, workdir: Path) -> str:
    inputs = [_describe_input(port, registry, workdir) for port in agent.consumes]
    return _env.get_template("inputs.md.j2").render(inputs=inputs)


def _describe_input(
    port: Port, registry: ArtifactRegistry, workdir: Path
) -> dict[str, object]:
    port_dir = workdir / "input" / port.port
    inner_name, is_collection = parse_type_ref(port.type)
    rtype = registry.get(inner_name)

    collection_manifest = port_dir / "_collection.json"
    item_marker = port_dir / "_item.json"

    if is_collection or collection_manifest.exists():
        return _describe_collection(port, port_dir, workdir, collection_manifest)
    if item_marker.exists():
        return {
            "port": port.port,
            "type": port.type,
            "path": _rel(port_dir, workdir),
            "content": None,
            "note": "A single map element; see `_item.json` for its provenance.",
        }

    # single file or dir/any
    content: str | None = None
    note: str | None = None
    file_path = port_dir / artifact_filename(port.port, rtype) if rtype else None
    if rtype is not None and rtype.kind is TypeKind.file and file_path is not None:
        display_path = _rel(file_path, workdir)
        if file_path.exists():
            size = file_path.stat().st_size
            if rtype.should_inline(size):
                content = file_path.read_text("utf-8")
    else:
        display_path = _rel(port_dir, workdir)
        note = "Directory input; read its contents."
    return {
        "port": port.port,
        "type": port.type,
        "path": display_path,
        "content": content,
        "note": note,
    }


def _describe_collection(
    port: Port, port_dir: Path, workdir: Path, manifest: Path
) -> dict[str, object]:
    rel = _rel(port_dir, workdir)
    lines = [
        f"This is a **collection** ({port.type}). Each ok item's payload lives in its "
        f"own directory `{rel}/<slug>/` (the `source` field is only the original "
        "document's name, not a path). The index and — for small collections — each "
        "item's content are inlined below."
    ]
    if manifest.exists():
        data = json.loads(manifest.read_text("utf-8"))
        items = data.get("items", [])
        capped = len(items) > _COLLECTION_INLINE_MAX_ITEMS
        shown = (
            {**data, "items": items[:_COLLECTION_INLINE_MAX_ITEMS]} if capped else data
        )
        suffix = (
            f" (first {_COLLECTION_INLINE_MAX_ITEMS} of {len(items)})" if capped else ""
        )
        lines.append(
            f"\nIndex{suffix}:\n```json\n"
            f"{json.dumps(shown, ensure_ascii=False, indent=2)}\n```"
        )
        if not capped:
            for item in items:
                if item.get("status") != "ok":
                    continue
                payload = _inline_element(port_dir / str(item.get("slug", "")))
                if payload:
                    lines.append(
                        f"\nItem `{item.get('slug')}` (from {item.get('source')}):"
                        f"\n```\n{payload}\n```"
                    )
        else:
            lines.append(
                f"\nToo many items to inline — read each ok item's file(s) under "
                f"`{rel}/<slug>/`."
            )
    return {
        "port": port.port,
        "type": port.type,
        "path": rel,
        "content": None,
        "note": "\n".join(lines),
    }


def _inline_element(slug_dir: Path) -> str | None:
    """Concatenate a collection element's payload file(s) for inlining (§11).

    Reads the ordinary files directly under the element dir (skips ``_item.json``),
    each capped so one big element can't blow up the prompt. Binary/oversized →
    a short marker instead of raw bytes.
    """
    if not slug_dir.is_dir():
        return None
    chunks: list[str] = []
    for f in sorted(slug_dir.iterdir()):
        if not f.is_file() or f.name == "_item.json":
            continue
        size = f.stat().st_size
        if size > INLINE_ELEMENT_MAX_BYTES:
            chunks.append(f"[{f.name}: {size} bytes — read the file directly]")
            continue
        try:
            chunks.append(f.read_text("utf-8"))
        except (OSError, UnicodeDecodeError):
            chunks.append(
                f"[{f.name}: {size} bytes, non-text — read the file directly]"
            )
    return "\n".join(c for c in chunks if c) or None


# --- outputs (SPEC §11 item 3) ---------------------------------------------


def _render_outputs(
    agent: AgentSpec, registry: ArtifactRegistry, gate_rules: Sequence[Rule] = ()
) -> str:
    outputs = []
    for i, port in enumerate(agent.produces):
        rtype = registry.get(port.type)
        rel = (
            f"output/{artifact_filename(port.port, rtype)}"
            if rtype
            else f"output/{port.port}"
        )
        # node-level rules apply to the primary port; the agent has to be told what
        # it is actually held to, and that text is generated, never hand-written (I5)
        extra = gate_rules if i == 0 else ()
        outputs.append(
            {
                "port": port.port,
                "type": port.type,
                "optional": port.optional,
                "path": rel,
                "summary": _schema_summary(registry, port.type, extra),
            }
        )
    return _env.get_template("outputs.md.j2").render(outputs=outputs)


def _schema_summary(
    registry: ArtifactRegistry, type_name: str, extra_rules: Sequence[Rule] = ()
) -> str:
    rtype = registry.get(type_name)
    if rtype is None:
        return ""
    # where a `forbid_file` list is read from, so the agent is shown the patterns
    # themselves rather than the name of a file it cannot open (I1)
    base_dir = registry.library_path
    lines: list[str] = []
    fmt = rtype.format.value if rtype.format is not None else rtype.kind.value
    lines.append(f"Format: {fmt}.")
    if rtype.schema is not None:
        # Show the FULL JSON Schema, not just field names — the agent must emit
        # exactly this shape (nested item structure included), and the gate
        # validates against it. Generated from the contract (I5).
        lines.append(
            "The JSON MUST validate against this JSON Schema:\n```json\n"
            + json.dumps(rtype.schema, indent=2, ensure_ascii=False)
            + "\n```"
        )
    # Every rule the gate will run, stated here. A rule the agent is not told about is a
    # rule it can only discover by failing: the gate retries with feedback, but that
    # retry is a whole draft paid for twice. Silence here also produced the opposite
    # defect — a live run's critic spent a remark on length in each of its three rounds
    # while the ceiling sat in the pipeline, unseen by the writer and unmeasured by
    # anyone. Generated from the contract, never hand-written into a prompt (I5).
    for rule in [*rtype.rules, *extra_rules]:
        if isinstance(rule, RegexRule):
            lines.append(f"Must match regex `{rule.pattern}`.")
        elif isinstance(rule, ForbidRegexRule):
            allowance = (
                "must not appear at all"
                if rule.max_hits == 0
                else f"may appear at most {rule.max_hits} time(s)"
            )
            lines.append(f"The pattern `{rule.pattern}` {allowance}.")
        elif isinstance(rule, ForbidFileRule):
            patterns, problems = load_forbid_patterns(Path(rule.path), base_dir)
            if problems:
                # Announced rather than skipped: an agent told nothing about a list that
                # failed to load would write against a policy nobody stated, and the gate
                # would then fail it for a reason it never saw.
                lines.append(
                    f"NOTE: the ban list `{rule.path}` could not be read "
                    f"({problems[0]}); the gate will fail on it."
                )
            else:
                shown = ", ".join(f"`{p}`" for _, p in patterns)
                allowance = (
                    "None of these patterns may appear"
                    if rule.max_hits == 0
                    else f"Each of these may appear at most {rule.max_hits} time(s)"
                )
                lines.append(
                    f"{allowance} (regexes, case-insensitive, from `{rule.path}`): "
                    f"{shown}."
                )
        elif isinstance(rule, MinLengthRule):
            lines.append(f"At least {rule.value} characters.")
        elif isinstance(rule, MaxLengthRule):
            lines.append(f"At most {rule.value} characters.")
        elif isinstance(rule, ProseCharsRule):
            bounds = []
            if rule.min is not None:
                bounds.append(f"at least {rule.min}")
            if rule.max is not None:
                bounds.append(f"at most {rule.max}")
            lines.append(
                f"Readable prose: {' and '.join(bounds)} characters. Counted with code "
                "blocks, inline code, tables, image placeholders and URLs removed and "
                "whitespace collapsed — so the count is smaller than the file, and "
                "moving text into a code block does not buy room."
            )
        elif isinstance(rule, NoEmptySectionsRule):
            lines.append(
                "Every heading must have text under it. A heading followed directly by a "
                "deeper heading is fine; a heading followed by nothing but a sibling is "
                "an empty promise."
            )
        elif isinstance(rule, MinEntriesRule):
            lines.append(f"At least {rule.value} entries in the directory.")
        elif isinstance(rule, CitationClosureRule):
            lines.append(
                f"Every `[n]` reference in the text must resolve to an entry of the "
                f"numbered list under `{rule.list_heading}`, every entry must be cited "
                f"at least once, and the list must be numbered 1..N without gaps."
            )
            if rule.min_entry_chars:
                lines.append(
                    f"Each entry must be at least {rule.min_entry_chars} characters — "
                    f"enough to identify the source, not a stub."
                )
    return " ".join(lines)


def _rel(path: Path, workdir: Path) -> str:
    """Path relative to the workdir, forward slashes (I1: only relative paths)."""
    return path.relative_to(workdir).as_posix()
