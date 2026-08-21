"""HITL end to end (SPEC §16.9, phase 3): a valid question@v1 parks the run
waiting_human; a human answer + resume lets the step proceed to completed.

MockRuntime only. Drives run_pipeline directly (mirrors tests/test_loop.py).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from refract.cli import write_answer
from refract.events import EventWriter
from refract.graph import load_agents
from refract.models.ledger import NodeStatus, RunStatus, StepStatus
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import run_pipeline
from refract.state import Ledger

from reqdoc import requirements_doc

_TYPES = """
version: "0.1"
types:
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
"""
DOC = requirements_doc("R")


async def _no_sleep(_seconds: float) -> None:
    return None


def _setup(tmp_path: Path) -> tuple:
    lib = tmp_path / "library"
    (lib / "types" / "schemas").mkdir(parents=True)
    (lib / "types" / "artifact_types.yaml").write_text(_TYPES, encoding="utf-8")
    d = lib / "agents" / "asker"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "asker",
                "version": 1,
                "consumes": [],
                "produces": [
                    {"port": "doc", "type": "requirements@v1"},
                    {"port": "q", "type": "question@v1", "optional": True},
                ],
                "needs": ["read", "edit"],
            }
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text("You are asker.", encoding="utf-8")
    agents, errs = load_agents(lib)
    assert errs == []
    reg = ArtifactRegistry.load(lib)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "hitl",
            "nodes": [
                {
                    "id": "ask",
                    "type": "agent",
                    "agent": "asker@1",
                    "params": {"model": "m/m"},
                }
            ],
        }
    )
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    for ref in agents:
        shutil.copytree(
            lib / "agents" / ref.split("@")[0], run_dir / "snapshot" / "agents" / ref
        )
    return agents, reg, pl, run_dir


def test_question_pauses_then_answer_completes(tmp_path: Path) -> None:
    agents, reg, pl, run_dir = _setup(tmp_path)
    ledger = Ledger.create(
        run_dir, run_id="r", pipeline="hitl", node_ids=["ask"], created_at="T0"
    )
    # same runtime instance across both runs: turn 1 asks, turn 2 (after answer) answers
    runtime = MockRuntime(
        {
            "ask": [
                ScriptedResponse(
                    files={"q.json": json.dumps({"question": "which db?"})}
                ),
                ScriptedResponse(files={"doc.md": DOC}),
            ]
        }
    )

    async def _run(led: Ledger) -> RunStatus:
        return await run_pipeline(
            run_dir,
            pipeline=pl,
            agents=agents,
            registry=reg,
            runtime=runtime,
            ledger=led,
            events=EventWriter(run_dir),
            clock=lambda: "T",
            sleeper=_no_sleep,
        )

    # turn 1 → the agent asks → run parks
    status1 = asyncio.run(_run(ledger))
    assert status1 is RunStatus.waiting_human
    assert ledger.get_node("ask").status is NodeStatus.waiting_human
    assert ledger.get_step("ask").status is StepStatus.waiting_human
    assert (run_dir / "steps" / "ask" / "main" / "hitl" / "question.json").exists()

    # human answers, then resume (reload ledger like a real resume)
    write_answer(run_dir, "ask", "use Postgres")
    reloaded = Ledger.load(run_dir)
    status2 = asyncio.run(_run(reloaded))
    assert status2 is RunStatus.completed
    assert reloaded.get_node("ask").status is NodeStatus.done
    assert (run_dir / "steps" / "ask" / "main" / "output" / "doc.md").read_text(
        "utf-8"
    ) == DOC
    # the answer was consumed and the question attempt archived
    assert not (run_dir / "steps" / "ask" / "main" / "hitl" / "answer.json").exists()


def _run_once(run_dir, pl, agents, reg, runtime, ledger):
    async def go():
        return await run_pipeline(
            run_dir,
            pipeline=pl,
            agents=agents,
            registry=reg,
            runtime=runtime,
            ledger=ledger,
            events=EventWriter(run_dir),
            clock=lambda: "T",
            sleeper=_no_sleep,
        )

    return asyncio.run(go())


def test_gate_retry_after_answer_does_not_collide(tmp_path: Path) -> None:
    # Regression: after a HITL answer archives the question turn to attempts/1, a
    # gate retry must allocate the next free slot (not reuse attempts/1) or the run
    # crashes. Answer → bad output (fails gate) → retry → good output → completed.
    agents, reg, pl, run_dir = _setup(tmp_path)
    ledger = Ledger.create(
        run_dir, run_id="r", pipeline="hitl", node_ids=["ask"], created_at="T0"
    )
    runtime = MockRuntime(
        {
            "ask": [
                ScriptedResponse(files={"q.json": json.dumps({"question": "?"})}),
                ScriptedResponse(files={"doc.md": "no header, fails the rule"}),
                ScriptedResponse(files={"doc.md": DOC}),
            ]
        }
    )
    assert (
        _run_once(run_dir, pl, agents, reg, runtime, ledger) is RunStatus.waiting_human
    )
    write_answer(run_dir, "ask", "answer")
    status = _run_once(run_dir, pl, agents, reg, runtime, Ledger.load(run_dir))
    assert status is RunStatus.completed  # no attempts/ collision
    assert ledger.get_node("ask") is not None
    base = run_dir / "steps" / "ask" / "main" / "attempts"
    assert (base / "1").is_dir() and (base / "2").is_dir()  # question + bad turn


def test_multi_turn_reask_parks_again(tmp_path: Path) -> None:
    # A fresh question after an answer re-parks the run (SPEC §16.9 multi-turn).
    agents, reg, pl, run_dir = _setup(tmp_path)
    ledger = Ledger.create(
        run_dir, run_id="r", pipeline="hitl", node_ids=["ask"], created_at="T0"
    )
    runtime = MockRuntime(
        {
            "ask": [
                ScriptedResponse(files={"q.json": json.dumps({"question": "q1?"})}),
                ScriptedResponse(files={"q.json": json.dumps({"question": "q2?"})}),
                ScriptedResponse(files={"doc.md": DOC}),
            ]
        }
    )
    assert (
        _run_once(run_dir, pl, agents, reg, runtime, ledger) is RunStatus.waiting_human
    )
    write_answer(run_dir, "ask", "a1")
    assert (
        _run_once(run_dir, pl, agents, reg, runtime, Ledger.load(run_dir))
        is RunStatus.waiting_human
    )  # re-asked → parked again
    write_answer(run_dir, "ask", "a2")
    assert (
        _run_once(run_dir, pl, agents, reg, runtime, Ledger.load(run_dir))
        is RunStatus.completed
    )
