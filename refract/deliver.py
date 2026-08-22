"""Assemble a run's declared deliverables into ``runs/<id>/output/`` (SPEC §22).

The pipeline says WHAT it delivers and under WHICH name (``outputs`` in
``pipeline.yaml``); this puts those artifacts where a person looks for them without
walking the step tree. Until this existed the result of a run lived at
``runs/<id>/steps/restyle/main/output/article.md`` beside its pictures three directories
away, and getting a readable article out meant copying by hand — which also meant the
next run silently overwrote whatever the copy had landed in.

Two rules the assembly keeps:

* the NAME comes from the pipeline, not from the port. An article carrying
  ``![](figures/<slug>.png)`` needs that directory called ``figures`` next to it, and
  only the pipeline knows the promise the artifact made;
* a missing artifact is reported, never skipped quietly. A delivery folder holding three
  of four things looks exactly like a complete one.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from refract.artifacts import (
    UNRESOLVED_FILENAME,
    artifact_filename,
    link_or_copy,
    long_path,
    node_output_base,
)
from refract.graph import DataRef, ValidationContext, _Validator, parse_ref
from refract.models.agent import AgentSpec
from refract.models.pipeline import Node, Pipeline
from refract.registry import ArtifactRegistry

# The run's delivery: `runs/<id>/result/`. Named `result`, not `output`, because `output`
# already means the outbox of one STEP (`steps/<id>/output/`) — the same word on two levels
# for two different things, which is most of why the run tree read as a maze.
OUTPUT_DIRNAME = "result"
# A mirror of the newest run's delivery, in the project root where a person looks. Named
# `latest`, not `result`: it is overwritten by the next run, and a directory called `result`
# invites the belief that results accumulate there. Each run keeps its own under
# `runs/<id>/result/`, untouched by anything that happens afterwards.
RESULT_DIRNAME = "latest"


@dataclass
class DeliveryReport:
    """What was delivered and what could not be (SPEC §22)."""

    output_dir: Path
    delivered: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    # Where the same content was mirrored for a person to open (``<project>/result/``),
    # or None when nothing was delivered or publishing was switched off.
    result_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.missing

    def render(self) -> list[str]:
        lines = [f"{name} -> {rel}" for name, rel in sorted(self.delivered.items())]
        lines += [
            f"MISSING {name}: {why}" for name, why in sorted(self.missing.items())
        ]
        return lines





def _deliver_unresolved(run_dir: Path, out_dir: Path, report: DeliveryReport) -> None:
    """Carry every loop's open-items report into the delivery (SPEC §22).

    Not declared in ``outputs`` and deliberately so: this is the ENGINE's record of what a
    loop could not close, written from the typed verdict, not an artifact any agent
    produced. It travels with the deliverable because that is the only place a person
    looks — two live runs shipped articles carrying real, unaddressed remarks whose only
    trace was one warning line in the event log.

    One loop delivers ``unresolved.md``; several deliver ``unresolved/<node>.md``, because
    silently merging two nodes' findings into one file loses which stage said what.
    """
    found = sorted(run_dir.glob(f"steps/*/_out/{UNRESOLVED_FILENAME}"))
    if not found:
        return
    if len(found) == 1:
        link_or_copy(found[0], out_dir / UNRESOLVED_FILENAME)
        report.delivered["unresolved"] = f"{OUTPUT_DIRNAME}/{UNRESOLVED_FILENAME}"
        return
    for src in found:
        node_id = src.parent.parent.name
        link_or_copy(src, out_dir / "unresolved" / f"{node_id}.md")
    report.delivered["unresolved"] = f"{OUTPUT_DIRNAME}/unresolved/ ({len(found)})"


def _collection_unusable(src: Path) -> str | None:
    """Why a collection directory cannot be delivered, or ``None``.

    Non-emptiness is not enough for a collection: the manifest itself is always there, so a
    run whose every element failed delivers a directory holding `_collection.json` and
    nothing else — and reports it as delivered. The manifest is the one thing that knows
    the difference.
    """
    manifest = src / "_collection.json"
    if not manifest.exists():
        return "produced a collection with no _collection.json"
    try:
        data = json.loads(manifest.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"produced an unreadable _collection.json ({exc})"
    items = data.get("items") if isinstance(data, dict) else None
    ok = [i for i in items if isinstance(i, dict) and i.get("status") == "ok"] if isinstance(items, list) else []
    if not ok:
        return "produced a collection whose every element failed"
    return None


def _unusable(src: Path, *, is_collection: bool = False) -> str | None:
    """Why this artifact cannot be delivered, or ``None`` if it can.

    Existence is not enough, and the gate already knows it: a ``dir`` artifact is gated on
    having real content, because an agent that produced nothing still leaves a directory.
    Delivery checked existence alone and so reported an EMPTY figures directory as
    delivered — the exact failure this module's docstring says it exists to prevent, one
    level down. Same rule as the gate: dot-entries are tooling, not content.
    """
    if not src.exists():
        return "was not produced"
    if src.is_dir():
        if is_collection:
            return _collection_unusable(src)
        # A real FILE somewhere in the tree. A directory whose only child is another empty
        # directory passed the old check, which counted entries at the top level only.
        if not any(p.is_file() and not p.name.startswith(".") for p in src.rglob("*")):
            return "produced an empty directory"
        return None
    if src.stat().st_size == 0:
        return "produced an empty file"
    return None


def _port_types(
    pipeline: Pipeline, registry: ArtifactRegistry, agents: dict[str, AgentSpec]
) -> dict[str, dict[str, str]]:
    """Port -> type for every node, resolved by the graph validator itself.

    Reusing the validator rather than re-deriving the mapping here: it already knows how
    map wraps a port in ``collection<>``, what a loop exports, and that a discover node
    exposes one engine-made port. A second implementation of that would be a second
    answer to the same question.
    """
    ctx = ValidationContext(registry=registry, agents=agents)
    v = _Validator(pipeline, ctx)
    v.nodes = {n.id: n for n in pipeline.nodes}
    return {node_id: v.producer_types(node_id) for node_id in v.nodes}


def _source_of(
    run_dir: Path,
    registry: ArtifactRegistry,
    nodes: dict[str, Node],
    ptypes: dict[str, dict[str, str]],
    ref: DataRef,
) -> tuple[Path, bool, bool] | None:
    """Where the port was written, whether it is a directory, and whether a collection."""
    node = nodes.get(ref.node_id)
    if node is None:
        return None
    base = node_output_base(run_dir, node)
    type_name = ptypes.get(ref.node_id, {}).get(ref.port)
    if type_name is not None and type_name.startswith("collection<"):
        return base / ref.port, True, True
    if type_name is None:
        # a port the graph could not type: treat as a plain directory named after it
        return base / ref.port, True, False
    rtype = registry.get(type_name)
    if rtype is None:
        return base / ref.port, True, False
    return (
        base / artifact_filename(ref.port, rtype),
        rtype.kind.value != "file",
        False,
    )


def _project_root(run_dir: Path) -> Path | None:
    """The project a run belongs to: the parent of the ``runs/`` directory holding it.

    Found by name rather than by counting levels up. ``run_dir.parent.parent`` gives the
    right answer for ``<project>/runs/<id>/`` and a silently wrong one for anything else —
    and "anything else" includes every test fixture and every hand-made directory someone
    points the CLI at. Writing a `result/` outside the project is not a bug you notice.
    """
    parent = run_dir.parent
    if parent.name != "runs":
        return None
    return parent.parent


def publish_result(run_dir: Path, report: DeliveryReport) -> Path | None:
    """Mirror this run's delivery into ``<project>/result/`` (SPEC §22).

    The run's own ``output/`` stays where it is: it belongs to that run and must not change
    when the next one finishes. ``result/`` is the other half of the same idea — the current
    answer to "where is it", in the project root where a person actually looks, rather than
    four levels down under a directory named after a timestamp.

    A copy, not a link: this directory is what gets zipped and sent, and a link would travel
    as a broken pointer. Rebuilt from scratch, so a run that stops delivering something does
    not leave the previous run's version of it sitting there looking current.
    """
    out_dir = run_dir / OUTPUT_DIRNAME
    if not out_dir.is_dir() or not any(out_dir.iterdir()):
        return None
    project_dir = _project_root(run_dir)
    if project_dir is None:
        return None
    result_dir = project_dir / RESULT_DIRNAME
    if result_dir.exists():
        shutil.rmtree(long_path(result_dir))
    shutil.copytree(long_path(out_dir), long_path(result_dir))
    (result_dir / "FROM_RUN.txt").write_text(
        f"{run_dir.name}\n", encoding="utf-8"
    )
    report.result_dir = result_dir
    return result_dir


def deliver(
    run_dir: Path | str,
    *,
    pipeline: Pipeline,
    registry: ArtifactRegistry,
    agents: dict[str, AgentSpec] | None = None,
    publish: bool = True,
) -> DeliveryReport:
    """Copy every declared output into ``runs/<id>/output/`` (SPEC §22).

    Rebuilt from scratch on every call so a resumed or rerun delivery cannot leave a
    stale artifact from an earlier attempt sitting beside a fresh one.

    With ``publish``, the same content is mirrored into ``<project>/result/`` — see
    ``publish_result`` for why both exist.
    """
    run_dir = Path(run_dir)
    out_dir = run_dir / OUTPUT_DIRNAME
    report = DeliveryReport(output_dir=out_dir)
    # No declared outputs still leaves the engine's own record to hand over: a pipeline whose
    # result is read in place can still have a loop that left items open.
    if not pipeline.outputs and not any(run_dir.glob(f"steps/*/_out/{UNRESOLVED_FILENAME}")):
        return report

    nodes: dict[str, Node] = {n.id: n for n in pipeline.nodes}
    ptypes = _port_types(pipeline, registry, agents or {})
    if out_dir.exists():
        shutil.rmtree(long_path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    _deliver_unresolved(run_dir, out_dir, report)

    for name, ref_s in pipeline.outputs.items():
        ref = parse_ref(ref_s)
        if not isinstance(ref, DataRef):
            report.missing[name] = f"{ref_s!r} is not a <node>.<port> reference"
            continue
        found = _source_of(run_dir, registry, nodes, ptypes, ref)
        if found is None:
            report.missing[name] = f"unknown node {ref.node_id!r}"
            continue
        src, is_dir, is_collection = found
        why = _unusable(src, is_collection=is_collection)
        if why is not None:
            report.missing[name] = f"{ref_s} {why} ({src})"
            continue
        # the pipeline's name, plus the artifact's own extension for a file: an article
        # delivered as `article` with no suffix is a file nothing will open as markdown
        dst = out_dir / (name if is_dir else name + "".join(Path(src.name).suffixes))
        link_or_copy(src, dst)
        report.delivered[name] = dst.relative_to(run_dir).as_posix()
    if publish:
        publish_result(run_dir, report)
    return report
