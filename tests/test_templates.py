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

from reqdoc import requirements_doc
from designdoc import design_doc

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
        known_mcp_servers={"tavily-remote", "pdf-reader"},
    )


def test_requirements_type_rejects_front_matter(tmp_path: Path) -> None:
    """A live run had the writer prepend YAML front matter with invented counts.

    The prose asks it not to; this rule makes the gate settle it (SPEC §10.2).
    """
    from refract.artifacts import GatePort, check_port

    registry = ArtifactRegistry.load(LIBRARY)
    rtype = registry.get("requirements@v1")
    assert rtype is not None
    body = requirements_doc("T")
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
        "explainer_article",
    ],
)
def test_template_validates(name: str) -> None:
    graph = load_pipeline(TEMPLATES / f"{name}.yaml", _ctx())
    assert graph.ok, [(e.code.value, e.node_id, e.message) for e in graph.errors]
    assert graph.order


# --- end-to-end execution on MockRuntime (SPEC §17 Phase-1 criterion) --------

_REQ = requirements_doc("T")
# design_doc@v1 now requires a real body and a declared assumptions section (SPEC §5),
# so the scripted output must clear the same gate a real agent faces
_DESIGN = design_doc("T")
_REPORT = "# Discovery\nOpen questions and unknowns.\n"
_APPROVED = json.dumps({"verdict": "approved"})
_EXTRACT = json.dumps({"source": "s", "source_type": "brief", "requirements": [], "trust_level": "low"})

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
# --- explainer_article fixtures ---------------------------------------------
# article@v1 gates on three things: an H1 at the very start, at least one figure
# placeholder (the contract the illustrator fulfils), and a genre floor of 6000 chars.
# The prose deliberately contains none of the calques the `restyle` node forbids.
# The article declares four figures; the illustrator owes exactly these four filenames.
_FIGURE_SLUGS = ("x-to-qkv", "scores-heatmap", "softmax-weighted-sum", "multi-head")
_ARTICLE = (
    "# Механизм внимания\n\n"
    + "".join(
        f"![Схема {i}: как это работает](figures/{slug}.png)\n\n"
        for i, slug in enumerate(_FIGURE_SLUGS, start=1)
    )
    + (
        "Каждый токен получает три вектора, и каждый из них отвечает за свою роль "
        "в вычислении. Скалярное произведение показывает, насколько один вектор "
        "похож на другой, а нормировка удерживает значения в разумных пределах. "
    )
    * 40
)
# style_findings@v1: a clean text is a real outcome — the schema requires the summary
# and the list, not that the list be non-empty
_FINDINGS = json.dumps(
    {
        "summary": "Текст ровный, механика чистая, ритм без серий одинаковых фраз.",
        "counters": {"dash": 0, "quotes": 0, "address_ty": 0},
        "findings": [],
    },
    ensure_ascii=False,
)
_PNG = b"\x89PNG" + b"0" * 64
_MANIFEST = json.dumps(
    {
        "figures": [
            {
                "slug": "x-to-qkv",
                "caption": "Одна матрица X превращается в Q, K и V тремя проекциями",
                "file": "x-to-qkv.png",
                "command": "paperbanana generate --input figure-x-to-qkv.txt ...",
                "status": "ok",
            }
        ],
        "failed": [],
    },
    ensure_ascii=False,
)


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
    "explainer_article": {
        # min_sources: 12 — the floor the finder must clear or the node fails
        "find": {
            f"found/source-{i}.md": f"# Source {i}\nSubstance.\n" for i in range(1, 13)
        },
        "study:*": {"note.json": _NOTE},
        "analyse": {"analysis.json": _ANALYSIS},
        # a CHAIN body: the writer drafts, the verifier hands back the same article with
        # its arithmetic corrected — two step ids per round, not one
        "write.body1:*": {"article.md": _ARTICLE},
        "write.body2:*": {"article.md": _ARTICLE},
        # third link: the fact checker, which also hands back the article. Three step ids
        # per round, and each of them a chance for the chain to lose the draft.
        "write.body3:*": {"article.md": _ARTICLE},
        "write.critic:*": {"verdict.json": _APPROVED},
        "style": {"findings.json": _FINDINGS},
        "restyle": {"article.md": _ARTICLE},
        # four figures and the manifest: the node's own `min_entries: 5`, which is what
        # keeps "produced one of the four it owed" from passing as a non-empty directory
        "figures": {
            **{f"illustration/{slug}.png": _PNG for slug in _FIGURE_SLUGS},
            "illustration/manifest.json": _MANIFEST,
        },
    },
    "requirements_to_design": {
        "extract:*": {"extract.json": _EXTRACT},
        # the loop body is a CHAIN: the writer, then the fact checker that walks the
        # claims against the extracts — two step ids per round, not one
        # filenames match each agent's produce PORT, and the two differ: the writer
        # produces `requirements`, the fact checker `doc`
        "refine.body1:*": {"requirements.md": _REQ},
        "refine.body2:*": {"doc.md": _REQ},
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
        "explainer_article",
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


def _run_explainer(tmp_path, *, restyle_article: str, figures: dict | None = None):
    """Run explainer_article with the checkpoint lifted, so the tail executes.

    The parametrized e2e stops at the checkpoint by design (SPEC §21), which leaves
    `restyle` and `figures` — the calque gate and the illustration directory — with no
    coverage at all. Lifting the checkpoint here exercises them without pretending the
    checkpoint is not part of the template.
    """
    agents, _ = load_agents(LIBRARY)
    registry = ArtifactRegistry.load(LIBRARY)
    raw = Pipeline.model_validate(
        yaml.safe_load((TEMPLATES / "explainer_article.yaml").read_text("utf-8"))
    )
    resolved = build_resolved(
        raw, agents=agents, overrides={}, default_model="claude/sonnet"
    )
    resolved["checkpoints"] = []
    pipeline = Pipeline.model_validate(resolved)

    proj_in = tmp_path / "input"
    proj_in.mkdir()
    (proj_in / "brief.md").write_text("Механизм внимания.", encoding="utf-8")
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    for ref in agents:
        shutil.copytree(
            LIBRARY / "agents" / ref.split("@")[0],
            run_dir / "snapshot" / "agents" / ref,
        )
    ledger = Ledger.create(
        run_dir,
        run_id="run_expl",
        pipeline="explainer_article",
        node_ids=[n.id for n in pipeline.nodes],
        created_at="T0",
    )
    scenario = dict(_SCENARIOS["explainer_article"])
    scenario["restyle"] = {"article.md": restyle_article}
    if figures is not None:
        scenario["figures"] = figures
    runtime = MockRuntime(
        {pat: [ScriptedResponse(files=f)] for pat, f in scenario.items()}
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
    return status, ledger, run_dir


def test_explainer_tail_produces_figures_for_the_declared_placeholders(tmp_path: Path):
    status, ledger, run_dir = _run_explainer(tmp_path, restyle_article=_ARTICLE)
    assert status is RunStatus.completed, {
        k: (v.status.value, v.error) for k, v in ledger.state.nodes.items()
    }
    figures = run_dir / "steps" / "figures" / "main" / "output" / "illustration"
    # every slug the article declared is a filename the illustrator owes
    for slug in _FIGURE_SLUGS:
        assert (figures / f"{slug}.png").exists(), slug
    manifest = json.loads((figures / "manifest.json").read_text("utf-8"))
    assert manifest["figures"][0]["slug"] == "x-to-qkv"
    # the loop body is a CHAIN: writer, then both correctors, in order
    assert "write.body1:r1" in ledger.state.steps
    assert "write.body2:r1" in ledger.state.steps
    assert "write.body3:r1" in ledger.state.steps


def test_explainer_calque_gate_bites_on_the_editor(tmp_path: Path):
    """The node's own terms, enforced mechanically: an editor that leaves a calque
    behind has not finished, and no critic round should be spent saying so."""
    dirty = _ARTICLE.replace(
        "Каждый токен получает", "Стоит отметить, что каждый токен получает", 1
    )
    status, ledger, _ = _run_explainer(tmp_path, restyle_article=dirty)
    assert status is RunStatus.failed
    restyle = ledger.get_node("restyle")
    assert restyle is not None and restyle.status is NodeStatus.failed
    # and the step that failed says which pattern it was, not just "invalid"
    step = ledger.get_step("restyle")
    assert step is not None and "стоит отметить" in (step.error or "").lower()
    # the figures node never ran: the article it would illustrate did not pass
    assert ledger.get_node("figures").status is NodeStatus.skipped


def test_explainer_min_entries_gate_catches_a_short_figure_set(tmp_path: Path):
    """One figure out of four is the classic silent thin pass (SPEC §5 min_entries).

    A `dir` port is otherwise gated on non-emptiness alone, so a step that produced a
    single PNG would report `ok` and the missing three would surface only when a human
    opened the article.
    """
    status, ledger, _ = _run_explainer(
        tmp_path,
        restyle_article=_ARTICLE,
        figures={
            "illustration/x-to-qkv.png": _PNG,
            "illustration/manifest.json": _MANIFEST,
        },
    )
    assert status is RunStatus.failed
    step = ledger.get_step("figures")
    assert step is not None
    assert "min_entries 5 not met (got 2)" in (step.error or "")
