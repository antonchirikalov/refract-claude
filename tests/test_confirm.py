"""Capability confirmation (SPEC §7/§16.10): a run pauses for a human to approve
a sensitive capability before the agent runs; approve → proceed, reject → fail.

Reuses the HITL waiting_human/answer machinery. MockRuntime only.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from refract.cli import _confirm_caps, _recover_confirm_caps, write_answer
from refract.events import EventWriter
from refract.graph import load_agents
from refract.models.agent import capability_tier, tier_at_least
from refract.models.config import ProjectConfig
from refract.models.ledger import NodeStatus, RunStatus
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.base import StepResult
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


@dataclass
class _Fixture:
    agents: dict
    registry: ArtifactRegistry
    pipeline: Pipeline
    run_dir: Path
    ledger: Ledger


def _fixture(tmp_path: Path) -> _Fixture:
    lib = tmp_path / "library"
    (lib / "types" / "schemas").mkdir(parents=True)
    (lib / "types" / "artifact_types.yaml").write_text(_TYPES, encoding="utf-8")
    d = lib / "agents" / "runner"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "runner",
                "version": 1,
                "consumes": [],
                "produces": [{"port": "doc", "type": "requirements@v1"}],
                "needs": ["read", "edit", "bash"],
            }
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text("You are runner.", encoding="utf-8")
    agents, errs = load_agents(lib)
    assert errs == []
    reg = ArtifactRegistry.load(lib)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "c",
            "nodes": [
                {
                    "id": "run",
                    "type": "agent",
                    "agent": "runner@1",
                    "params": {"model": "m/m"},
                }
            ],
        }
    )
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    shutil.copytree(
        lib / "agents" / "runner", run_dir / "snapshot" / "agents" / "runner@1"
    )
    ledger = Ledger.create(
        run_dir, run_id="r", pipeline="c", node_ids=["run"], created_at="T0"
    )
    return _Fixture(agents, reg, pl, run_dir, ledger)


def _run_confirm(fx: _Fixture, led: Ledger, runtime: object) -> RunStatus:
    async def go() -> RunStatus:
        return await run_pipeline(
            fx.run_dir,
            pipeline=fx.pipeline,
            agents=fx.agents,
            registry=fx.registry,
            runtime=runtime,
            ledger=led,
            events=EventWriter(fx.run_dir),
            confirm_capabilities={"bash"},
            clock=lambda: "T",
            sleeper=_no_sleep,
        )

    return asyncio.run(go())


class _Guarded:
    """Runs the agent, recording invocations, so a test can assert it never ran
    before approval."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_step(self, spec, on_event):  # type: ignore[no-untyped-def]
        self.calls += 1
        (spec.workdir / "output").mkdir(parents=True, exist_ok=True)
        (spec.workdir / "output" / "doc.md").write_text(DOC, encoding="utf-8")
        (spec.workdir / "raw.txt").write_text("x", encoding="utf-8")
        (spec.workdir / "agent.events.jsonl").write_text("", encoding="utf-8")
        return StepResult(completed=True)

    async def close(self) -> None:
        return None


def test_capability_tiers() -> None:
    assert capability_tier("read") == "safe"
    assert capability_tier("bash") == "dangerous"
    assert capability_tier("mcp:tavily-remote") == "moderate"
    assert tier_at_least("bash", "dangerous")
    assert not tier_at_least("read", "moderate")


def test_confirm_caps_policy() -> None:
    # explicit list
    cfg = ProjectConfig(version="0.1", name="p", confirm=["bash"])
    spec = _RunnerSpec()
    assert _confirm_caps(cfg, {"runner@1": spec}) == {"bash"}
    # tier threshold: dangerous -> only bash of {read,edit,bash}
    cfg2 = ProjectConfig(version="0.1", name="p", confirm_tier="dangerous")
    assert _confirm_caps(cfg2, {"runner@1": spec}) == {"bash"}
    # moderate threshold -> edit + bash (read is safe)
    cfg3 = ProjectConfig(version="0.1", name="p", confirm_tier="moderate")
    assert _confirm_caps(cfg3, {"runner@1": spec}) == {"edit", "bash"}


def _RunnerSpec():  # noqa: N802
    from refract.models.agent import AgentSpec, Port

    return AgentSpec(
        name="runner",
        version=1,
        produces=[Port(port="doc", type="requirements@v1")],
        needs=["read", "edit", "bash"],
    )


def test_confirm_pauses_then_approval_proceeds(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    rt = _Guarded()
    # turn 1: bash needs confirmation -> run parks, the agent never ran
    status1 = _run_confirm(fx, fx.ledger, rt)
    assert status1 is RunStatus.waiting_human
    assert fx.ledger.get_node("run").status is NodeStatus.waiting_human
    assert rt.calls == 0  # agent not invoked before approval
    assert (fx.run_dir / "steps" / "run" / "main" / "confirm" / "request.json").exists()

    # human approves, then resume -> node runs
    write_answer(fx.run_dir, "run", "approved")
    assert (
        fx.run_dir / "steps" / "run" / "main" / "confirm" / "decision.json"
    ).exists()
    rt2 = _Guarded()
    status2 = _run_confirm(fx, Ledger.load(fx.run_dir), rt2)
    assert status2 is RunStatus.completed
    assert rt2.calls == 1
    assert (fx.run_dir / "steps" / "run" / "main" / "output" / "doc.md").exists()


def test_confirm_rejection_fails_node(tmp_path: Path) -> None:
    """A non-affirmative answer rejects the capability and fails the node — the
    agent is never invoked (I4: the ``approved`` flag drives the decision, not the
    mere presence of an answer)."""
    fx = _fixture(tmp_path)
    rt = _Guarded()
    status1 = _run_confirm(fx, fx.ledger, rt)
    assert status1 is RunStatus.waiting_human

    write_answer(fx.run_dir, "run", "no")  # rejection
    rec = json.loads(
        (fx.run_dir / "steps" / "run" / "main" / "confirm" / "decision.json").read_text(
            "utf-8"
        )
    )
    assert rec["approved"] is False
    rt2 = _Guarded()
    status2 = _run_confirm(fx, Ledger.load(fx.run_dir), rt2)
    assert status2 is RunStatus.failed
    assert rt2.calls == 0  # agent never ran


def test_recover_confirm_caps_from_project_yaml(tmp_path: Path) -> None:
    """Resume recovers the confirm policy from project.yaml (not the snapshot)."""
    proj = tmp_path / "proj"
    (proj / "runs" / "r1").mkdir(parents=True)
    (proj / "project.yaml").write_text(
        yaml.safe_dump({"version": "0.1", "name": "p", "confirm_tier": "dangerous"}),
        encoding="utf-8",
    )
    run_dir = proj / "runs" / "r1"
    caps = _recover_confirm_caps(run_dir, {"runner@1": _RunnerSpec()})
    assert caps == {"bash"}
    # layout mismatch -> empty
    assert (
        _recover_confirm_caps(tmp_path / "elsewhere", {"runner@1": _RunnerSpec()})
        == set()
    )
