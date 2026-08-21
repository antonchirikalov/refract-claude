"""map_over fan-out over models + winner_model binding (SPEC §8.1/§10.3).

MockRuntime only. Mirrors the tests/test_select.py harness.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from refract.events import EventWriter
from refract.graph import load_agents
from refract.models.ledger import NodeStatus, RunStatus
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import node_dependencies, run_pipeline
from refract.state import Ledger

from reqdoc import requirements_doc

DOC = requirements_doc("d")
MODELS = ["kimi/kimi-k3", "openai/gpt-5.6"]

_TYPES = """
version: "0.1"
types:
  source@v1: { kind: any }
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
"""


async def _no_sleep(_seconds: float) -> None:
    return None


def _mk(lib: Path, name: str, consumes: list[dict], produces: list[dict]) -> None:
    d = lib / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": 1,
                "consumes": consumes,
                "produces": produces,
                "needs": ["read"],
            }
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text(f"You are {name}.", encoding="utf-8")


def _library(tmp_path: Path) -> tuple:
    lib = tmp_path / "library"
    (lib / "types" / "schemas").mkdir(parents=True)
    (lib / "types" / "artifact_types.yaml").write_text(_TYPES, encoding="utf-8")
    _mk(lib, "designer", [], [{"port": "design", "type": "requirements@v1"}])
    _mk(
        lib,
        "sel",
        [{"port": "cands", "type": "collection<requirements@v1>"}],
        [{"port": "choice", "type": "selection@v1"}],
    )
    _mk(
        lib,
        "refiner",
        [{"port": "draft", "type": "requirements@v1"}],
        [{"port": "doc", "type": "requirements@v1"}],
    )
    _mk(
        lib,
        "proc",
        [{"port": "src", "type": "source@v1"}],
        [{"port": "doc", "type": "requirements@v1"}],
    )
    agents, errs = load_agents(lib)
    assert errs == []
    return lib, agents, ArtifactRegistry.load(lib)


def _run(
    tmp_path: Path,
    pipeline: Pipeline,
    agents: dict,
    registry: ArtifactRegistry,
    scenario: dict,
    *,
    n_inputs: int = 0,
) -> tuple[RunStatus, Ledger, Path]:
    lib = tmp_path / "library"
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    for ref in agents:
        shutil.copytree(
            lib / "agents" / ref.split("@")[0], run_dir / "snapshot" / "agents" / ref
        )
    project_input: Path | None = None
    if n_inputs:
        project_input = tmp_path / "input"
        project_input.mkdir()
        for i in range(n_inputs):
            (project_input / f"{chr(ord('a') + i)}.txt").write_text(
                f"src {i}", encoding="utf-8"
            )
    ledger = Ledger.create(
        run_dir,
        run_id="r",
        pipeline=pipeline.name,
        node_ids=[n.id for n in pipeline.nodes],
        created_at="T0",
    )
    events = EventWriter(run_dir)
    status = asyncio.run(
        run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=agents,
            registry=registry,
            runtime=MockRuntime(scenario),
            ledger=ledger,
            events=events,
            project_input_dir=project_input,
            clock=lambda: "T",
            sleeper=_no_sleep,
        )
    )
    return status, ledger, run_dir


def _design_node(min_ok: int = 1, on_item_failure: str = "skip") -> dict:
    return {
        "id": "design",
        "type": "agent",
        "agent": "designer@1",
        "map_over": {"models": MODELS},
        "params": {"workers": 2, "min_ok": min_ok, "on_item_failure": on_item_failure},
    }


def test_map_over_fans_out_per_model(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {"version": "0.1", "name": "sd", "nodes": [_design_node()]}
    )
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {"design:*": [ScriptedResponse(files={"design.md": DOC})]},
    )
    assert status is RunStatus.completed
    manifest = json.loads(
        (
            run_dir / "steps" / "design" / "_out" / "design" / "_collection.json"
        ).read_text("utf-8")
    )
    assert manifest["type"] == "collection<requirements@v1>"
    assert [i["slug"] for i in manifest["items"]] == ["kimi_kimi-k3", "openai_gpt-5-6"]
    assert [i["source"] for i in manifest["items"]] == MODELS
    assert manifest["stats"] == {"total": 2, "ok": 2, "failed": 0}
    # one step per model
    assert "design:kimi_kimi-k3" in ledger.state.steps
    assert "design:openai_gpt-5-6" in ledger.state.steps


def test_map_over_min_ok_failure(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {"version": "0.1", "name": "sd", "nodes": [_design_node(min_ok=2)]}
    )
    # only the kimi model succeeds; openai errors → ok=1 < min_ok=2 → node fails
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "design:kimi_kimi-k3": [ScriptedResponse(files={"design.md": DOC})],
            "design:openai_gpt-5-6": [ScriptedResponse(agent_error="boom")],
        },
    )
    assert status is RunStatus.failed
    assert ledger.get_node("design").status is NodeStatus.failed
    manifest = json.loads(
        (
            run_dir / "steps" / "design" / "_out" / "design" / "_collection.json"
        ).read_text("utf-8")
    )
    assert manifest["stats"] == {"total": 2, "ok": 1, "failed": 1}


def test_winner_model_binding_end_to_end(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "sd",
            "nodes": [
                _design_node(),
                {
                    "id": "choose",
                    "type": "select",
                    "candidates": "design.design",
                    "selector": {"agent": "sel@1", "model": "kimi/kimi-k3"},
                    "params": {"fallback": "fail"},
                },
                {
                    "id": "refine",
                    "type": "agent",
                    "agent": "refiner@1",
                    "inputs": {"draft": "choose.out"},
                    "params": {"model": "@choose.winner_model"},
                },
            ],
        }
    )
    # winner_model binding creates a scheduling dependency on the select node
    assert "choose" in node_dependencies(pl)["refine"]

    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "design:*": [ScriptedResponse(files={"design.md": DOC})],
            "choose.selector": [
                ScriptedResponse(
                    files={"choice.json": json.dumps({"winner": "openai_gpt-5-6"})}
                )
            ],
            "refine": [ScriptedResponse(files={"doc.md": DOC})],
        },
    )
    assert status is RunStatus.completed
    assert ledger.get_node("choose").winner_model == "openai/gpt-5.6"
    # the refine step ran with the winner_model-resolved model
    raw = (run_dir / "steps" / "refine" / "main" / "raw.txt").read_text("utf-8")
    assert "openai/gpt-5.6" in raw


def test_map_node_model_resolves_winner_model_binding(tmp_path: Path) -> None:
    # A plain map node (source = scanner, not map_over) with a winner_model
    # binding must resolve the model per element (SPEC §8.1). Two branches:
    # design(map_over)→choose(select) yields winner_model; scan→proc(map) binds it.
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "sd",
            "nodes": [
                _design_node(),
                {
                    "id": "choose",
                    "type": "select",
                    "candidates": "design.design",
                    "selector": {"agent": "sel@1", "model": "kimi/kimi-k3"},
                    "params": {"fallback": "fail"},
                },
                {"id": "scan", "type": "builtin/scanner"},
                {
                    "id": "proc",
                    "type": "agent",
                    "agent": "proc@1",
                    "map": "scan.sources",
                    "params": {"model": "@choose.winner_model", "workers": 2},
                },
            ],
        }
    )
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "design:*": [ScriptedResponse(files={"design.md": DOC})],
            "choose.selector": [
                ScriptedResponse(
                    files={"choice.json": json.dumps({"winner": "openai_gpt-5-6"})}
                )
            ],
            "proc:*": [ScriptedResponse(files={"doc.md": DOC})],
        },
        n_inputs=2,
    )
    assert status is RunStatus.completed
    # each map element step ran with the winner_model-resolved model
    raw = (run_dir / "steps" / "proc" / "a-txt" / "raw.txt").read_text("utf-8")
    assert "openai/gpt-5.6" in raw
