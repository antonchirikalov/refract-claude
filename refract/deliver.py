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

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from refract.artifacts import (
    artifact_filename,
    link_or_copy,
    long_path,
    node_output_base,
)
from refract.graph import DataRef, ValidationContext, _Validator, parse_ref
from refract.models.agent import AgentSpec
from refract.models.pipeline import Node, Pipeline
from refract.registry import ArtifactRegistry

OUTPUT_DIRNAME = "output"


@dataclass
class DeliveryReport:
    """What was delivered and what could not be (SPEC §22)."""

    output_dir: Path
    delivered: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing

    def render(self) -> list[str]:
        lines = [f"{name} -> {rel}" for name, rel in sorted(self.delivered.items())]
        lines += [
            f"MISSING {name}: {why}" for name, why in sorted(self.missing.items())
        ]
        return lines


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
) -> tuple[Path, bool] | None:
    """Where the producer wrote this port, and whether it is a directory artifact."""
    node = nodes.get(ref.node_id)
    if node is None:
        return None
    base = node_output_base(run_dir, node)
    type_name = ptypes.get(ref.node_id, {}).get(ref.port)
    if type_name is None or type_name.startswith("collection<"):
        # a collection, or a port the graph could not type: both live in a directory
        # named after the port, which is what the assembly needs to know
        return base / ref.port, True
    rtype = registry.get(type_name)
    if rtype is None:
        return base / ref.port, True
    return base / artifact_filename(ref.port, rtype), rtype.kind.value != "file"


def deliver(
    run_dir: Path | str,
    *,
    pipeline: Pipeline,
    registry: ArtifactRegistry,
    agents: dict[str, AgentSpec] | None = None,
) -> DeliveryReport:
    """Copy every declared output into ``runs/<id>/output/`` (SPEC §22).

    Rebuilt from scratch on every call so a resumed or rerun delivery cannot leave a
    stale artifact from an earlier attempt sitting beside a fresh one.
    """
    run_dir = Path(run_dir)
    out_dir = run_dir / OUTPUT_DIRNAME
    report = DeliveryReport(output_dir=out_dir)
    if not pipeline.outputs:
        return report

    nodes: dict[str, Node] = {n.id: n for n in pipeline.nodes}
    ptypes = _port_types(pipeline, registry, agents or {})
    if out_dir.exists():
        shutil.rmtree(long_path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, ref_s in pipeline.outputs.items():
        ref = parse_ref(ref_s)
        if not isinstance(ref, DataRef):
            report.missing[name] = f"{ref_s!r} is not a <node>.<port> reference"
            continue
        found = _source_of(run_dir, registry, nodes, ptypes, ref)
        if found is None:
            report.missing[name] = f"unknown node {ref.node_id!r}"
            continue
        src, is_dir = found
        if not src.exists():
            report.missing[name] = f"{ref_s} was not produced ({src})"
            continue
        # the pipeline's name, plus the artifact's own extension for a file: an article
        # delivered as `article` with no suffix is a file nothing will open as markdown
        dst = out_dir / (name if is_dir else name + "".join(Path(src.name).suffixes))
        link_or_copy(src, dst)
        report.delivered[name] = dst.relative_to(run_dir).as_posix()
    return report
