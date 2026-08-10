"""Guard tests for the shipped Phase-1 pipeline templates (SPEC §4).

Each ``library/templates/*.yaml`` must validate against the shipped library
(registry + agent packages). Pure loading + graph validation — no MockRuntime.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from refract.events import EventWriter
from refract.graph import ValidationContext, load_agents, load_pipeline
from refract.models.ledger import NodeStatus, RunStatus
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import run_pipeline
from refract.snapshot import build_resolved
from refract.state import Ledger

LIBRARY = Path(__file__).resolve().parents[1] / "library"
TEMPLATES = LIBRARY / "templates"


def _ctx() -> ValidationContext:
    agents, errors = load_agents(LIBRARY)
    assert errors == [], errors
    return ValidationContext(
        registry=ArtifactRegistry.load(LIBRARY),
        agents=agents,
        # This fork runs on the Claude Code CLI, which serves the subscription's
        # models and nothing else: `model_alias()` strips the provider and hands the
        # rest to `--model`, so a non-claude provider is not a different vendor here,
        # it is an unrunnable pipeline. The shipped templates name only claude/*.
        known_providers={"claude"},
        available_providers={"claude"},
        default_model="claude/sonnet",
        # the servers the shipped agents need (~/.refract/mcp.yaml declares these)
        known_mcp_servers={"tavily-remote", "pdf-reader", "paperbanana"},
    )


def test_requirements_type_rejects_front_matter(tmp_path: Path) -> None:
    """A live run had the writer prepend YAML front matter with invented counts.

    The prose asks it not to; this rule makes the gate settle it (SPEC §10.2).
    """
    from refract.artifacts import GatePort, check_port

    registry = ArtifactRegistry.load(LIBRARY)
    rtype = registry.get("requirements@v1")
    assert rtype is not None
    body = "# Requirements: T\n\n- FR-1 alpha is testable.\n"
    for content, ok in (
        (body, True),
        ("---\nfr_count: 9\n---\n\n" + body, False),
    ):
        out = tmp_path / ("ok" if ok else "bad")
        out.mkdir()
        (out / "doc.md").write_text(content, encoding="utf-8")
        assert check_port(out, GatePort(port="doc", rtype=rtype)).ok is ok


def test_library_agents_load_without_errors() -> None:
    _, errors = load_agents(LIBRARY)
    assert errors == []


@pytest.mark.parametrize(
    "name",
    [
        "extract",
        "discovery",
        "solution_design",
        "research",
        "requirements_to_design",
        "analytic_report",
    ],
)
def test_template_validates(name: str) -> None:
    graph = load_pipeline(TEMPLATES / f"{name}.yaml", _ctx())
    assert graph.ok, [(e.code.value, e.node_id, e.message) for e in graph.errors]
    assert graph.order


# --- end-to-end execution on MockRuntime (SPEC §17 Phase-1 criterion) --------

_REQ = "# Requirements: T\n- FR-1 alpha\n"
# design_doc@v1 now requires a real body and a declared assumptions section (SPEC §5),
# so the scripted output must clear the same gate a real agent faces
_DESIGN = (
    "# Design\n\n## Approach\n\n"
    + ("A paragraph of solution design body text that carries weight. " * 40)
    + "\n\n## Risks and mitigations\n\n- Sync conflicts; supervisor review.\n"
    + "\n\n## Assumptions to confirm\n\n- Versions named are proposals.\n"
)
_REPORT = "# Discovery\nOpen questions and unknowns.\n"
_APPROVED = json.dumps({"verdict": "approved"})
_EXTRACT = json.dumps({"source": "s", "requirements": [], "trust_level": "low"})

# --- analytic_report fixtures -----------------------------------------------
# study_note@v1: the minimum a real note must carry to be usable downstream
_NOTE = json.dumps(
    {
        "source": "first",
        "source_kind": "primary_document",
        "primacy": "primary",
        "bibliography": {
            "entry": "Author A. A title long enough to identify the source. Publisher, 2026. URL: https://example.org/a",
            "incomplete_fields": ["pages"],
        },
        "key_points": [{"text": "The source establishes a thing.", "locator": "s. 2"}],
        "aspect_ids": ["1"],
        "trust_level": "high",
    }
)
# analysis@v1: one aspect, one finding traced to the note above
_ANALYSIS = json.dumps(
    {
        "aspects": [
            {
                "aspect": "1. The state of the matter",
                "established": [
                    {"statement": "A thing holds.", "notes": ["first"]},
                ],
                "implications": ["It will keep holding."],
                "material_gaps": [],
            }
        ],
        "cross_aspect": [{"observation": "The aspects share a cause."}],
    }
)
# analytic_report@v1 is gated on headings, length and citation closure, so the
# scripted draft has to clear the same gate a real writer faces
_ANALYTIC_REPORT = (
    "# ВСТУП\n\nThe subject and how the report proceeds [1].\n\n"
    "# 1. THE STATE OF THE MATTER\n\n"
    + ("A paragraph of substantive analysis carrying weight [1]. " * 400)
    + "\n\n# ВИСНОВКИ\n\nWhat was established, drawn together [1].\n\n"
    "# СПИСОК ВИКОРИСТАНИХ ДЖЕРЕЛ\n\n"
    "1. Author A. A title long enough to identify the source. Publisher, 2026. "
    "URL: https://example.org/a\n"
)

# Scripted outputs keyed by fnmatch step-id pattern; filenames match each agent's
# primary produce PORT (SPEC §10.4), not the loop output alias.
_SCENARIOS: dict[str, dict[str, dict[str, str]]] = {
    "extract": {
        "extract:*": {"extract.json": _EXTRACT},
        "refine.body:*": {"requirements.md": _REQ},
        "refine.critic:*": {"verdict.json": _APPROVED},
    },
    "discovery": {
        "extract:*": {"extract.json": _EXTRACT},
        "refine.body:*": {"requirements.md": _REQ},
        "refine.critic:*": {"verdict.json": _APPROVED},
        "probe": {"arch_report.md": "# Probes\n- What is the SLA target?\n"},
        "discover": {"report.md": _REPORT},
    },
    "research": {
        # the discover agent writes ONE dir; the engine makes the collection (§20)
        "find": {
            "found/first.md": "# First\nA source.\n",
            "found/second.md": "# Second\nAnother.\n",
            "found/third.md": "# Third\nOne more.\n",
        },
        "extract:*": {"extract.json": _EXTRACT},
        "refine.body:*": {"requirements.md": _REQ},
        "refine.critic:*": {"verdict.json": _APPROVED},
    },
    "analytic_report": {
        # min_sources: 8 — the finder must deliver its floor or the node fails
        "find": {
            f"found/source-{i}.md": f"# Source {i}\nSubstance.\n" for i in range(1, 9)
        },
        "study:*": {"note.json": _NOTE},
        # the stage between reading and writing: notes in, subject matter out
        "analyse": {"analysis.json": _ANALYSIS},
        "report.body:*": {"report.md": _ANALYTIC_REPORT},
        "report.critic:*": {"verdict.json": _APPROVED},
    },
    "requirements_to_design": {
        "extract:*": {"extract.json": _EXTRACT},
        "refine.body:*": {"requirements.md": _REQ},
        "refine.critic:*": {"verdict.json": _APPROVED},
        "design:*": {"design_doc.md": _DESIGN},
        "choose.selector": {"choice.json": json.dumps({"winner": "claude_opus"})},
        "sd_refine.body:*": {"design_doc.md": _DESIGN},
        "sd_refine.critic:*": {"verdict.json": _APPROVED},
    },
    "solution_design": {
        "extract:*": {"extract.json": _EXTRACT},
        "refine.body:*": {"requirements.md": _REQ},
        "refine.critic:*": {"verdict.json": _APPROVED},
        "design:*": {"design_doc.md": _DESIGN},
        "choose.selector": {"choice.json": json.dumps({"winner": "claude_opus"})},
        "sd_refine.body:*": {"design_doc.md": _DESIGN},
        "sd_refine.critic:*": {"verdict.json": _APPROVED},
    },
}


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    "name",
    [
        "extract",
        "discovery",
        "solution_design",
        "research",
        "requirements_to_design",
        "analytic_report",
    ],
)
def test_template_runs_end_to_end(name: str, tmp_path: Path) -> None:
    agents, _ = load_agents(LIBRARY)
    registry = ArtifactRegistry.load(LIBRARY)
    raw = Pipeline.model_validate(
        yaml.safe_load((TEMPLATES / f"{name}.yaml").read_text("utf-8"))
    )
    # resolve effective models (as the snapshot does) so map/loop nodes have one
    pipeline = Pipeline.model_validate(
        build_resolved(raw, agents=agents, overrides={}, default_model="claude/sonnet")
    )

    proj_in = tmp_path / "input"
    proj_in.mkdir()
    if pipeline.input_mode == "brief":
        # a brief pipeline reads one written brief instead of a document folder (§20.4)
        (proj_in / "brief.md").write_text("Research offline sync.", encoding="utf-8")
    else:
        (proj_in / "a.txt").write_text("Doc A", encoding="utf-8")
        (proj_in / "b.txt").write_text("Doc B", encoding="utf-8")
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    for ref in agents:
        shutil.copytree(
            LIBRARY / "agents" / ref.split("@")[0],
            run_dir / "snapshot" / "agents" / ref,
        )
    ledger = Ledger.create(
        run_dir,
        run_id="run_test",
        pipeline=name,
        node_ids=[n.id for n in pipeline.nodes],
        created_at="T0",
    )
    runtime = MockRuntime(
        {pat: [ScriptedResponse(files=f)] for pat, f in _SCENARIOS[name].items()}
    )
    status = asyncio.run(
        run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=agents,
            registry=registry,
            runtime=runtime,
            ledger=ledger,
            events=EventWriter(run_dir),
            project_input_dir=proj_in,
            clock=lambda: "T",
            sleeper=_no_sleep,
        )
    )
    if pipeline.checkpoints:
        # a checkpointed template parks for review instead of running to the end
        # (SPEC §21); continuing it is covered by tests/test_checkpoint.py
        assert status is RunStatus.waiting_human
        assert ledger.state.awaiting_checkpoint == pipeline.checkpoints[0]
        for node_id in pipeline.checkpoints:
            assert ledger.get_node(node_id).status in (
                NodeStatus.done,
                NodeStatus.reused,
            )
        return
    assert status is RunStatus.completed, {
        k: (v.status.value, v.error) for k, v in ledger.state.nodes.items()
    }
    assert all(
        n.status in (NodeStatus.done, NodeStatus.reused)
        for n in ledger.state.nodes.values()
    )
    if name == "research":
        # min_sources satisfied, collection assembled by the engine, map fanned out
        manifest = json.loads(
            (
                run_dir / "steps" / "find" / "_out" / "sources" / "_collection.json"
            ).read_text("utf-8")
        )
        assert manifest["stats"]["ok"] == 3  # min_sources: 3 in the template
        assert (run_dir / "steps" / "refine" / "_out" / "doc.md").exists()
    if name == "discovery":
        # arch_critic must see BOTH the draft and the requirements it curates
        # against — a live run showed it cannot judge redundancy against a
        # document it never receives.
        discover_in = run_dir / "steps" / "discover" / "main" / "input"
        assert (discover_in / "draft" / "draft.md").exists()
        assert (discover_in / "requirements" / "requirements.md").exists()
    if name == "solution_design":
        # the winner_model binding resolved and the final loop assembled its output
        assert ledger.get_node("choose").winner_model == "claude/opus"
        assert (run_dir / "steps" / "sd_refine" / "_out" / "design.md").exists()
