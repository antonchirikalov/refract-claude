"""Loop meta-node execution (SPEC §10.3 / §18 ``test_loop``).

MockRuntime only. Builds a tiny library on tmp_path (writer body + critic) and
drives ``run_pipeline`` directly, mirroring tests/test_map.py's harness.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from refract.events import EventWriter
from refract.graph import load_agents
from refract.models.ledger import NodeStatus, RunStatus, StepStatus
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.base import EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import run_pipeline
from refract.state import Ledger


class _BoomRuntime:
    """Fails the test if any step is executed (used to prove reuse on resume)."""

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        raise AssertionError(f"step {spec.step_id} should not run on resume")

    async def close(self) -> None:
        return None


DOC1 = "# Requirements: v1\n- FR-1 alpha\n"
DOC2 = "# Requirements: v2\n- FR-1 beta\n"
DOC3 = "# Requirements: v3\n- FR-1 gamma\n"

_TYPES = """
version: "0.1"
types:
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: regex, pattern: "^# Requirements:", flags: "m" }
"""


async def _no_sleep(_seconds: float) -> None:
    return None


def _mk_agent(lib: Path, name: str, consumes: list[dict], produces: list[dict]) -> None:
    d = lib / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": 1,
                "consumes": consumes,
                "produces": produces,
                "needs": ["read", "edit"],
            }
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text(f"You are {name}.", encoding="utf-8")


def _library(tmp_path: Path) -> tuple:
    lib = tmp_path / "library"
    (lib / "types" / "schemas").mkdir(parents=True)
    (lib / "types" / "artifact_types.yaml").write_text(_TYPES, encoding="utf-8")
    _mk_agent(lib, "writer", [], [{"port": "doc", "type": "requirements@v1"}])
    # a chain's second element: consumes a draft, produces the polished draft
    _mk_agent(
        lib,
        "polisher",
        [{"port": "draft", "type": "requirements@v1"}],
        [{"port": "doc", "type": "requirements@v1"}],
    )
    _mk_agent(
        lib,
        "critic",
        [{"port": "draft", "type": "requirements@v1"}],
        [{"port": "verdict", "type": "verdict@v1"}],
    )
    agents, errs = load_agents(lib)
    assert errs == []
    return lib, agents, ArtifactRegistry.load(lib)


def _loop_pipeline(
    *, max_rounds: int, on_max_rounds: str, gate_retries: int | None = None
) -> Pipeline:
    critic_block: dict = {"agent": "critic@1", "inputs": {"draft": "@body"}}
    params: dict = {
        "max_rounds": max_rounds,
        "on_max_rounds": on_max_rounds,
        "model": "kimi/kimi-k3",
    }
    if gate_retries is not None:
        params["gate_retries"] = gate_retries
    return Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "refine",
            "nodes": [
                {
                    "id": "refine",
                    "type": "loop",
                    "params": params,
                    "body": {"agent": "writer@1"},
                    "critic": critic_block,
                    "outputs": {"doc": "@body"},
                }
            ],
        }
    )


def _run(
    tmp_path: Path,
    pipeline: Pipeline,
    agents: dict,
    registry: ArtifactRegistry,
    scenario: dict,
) -> tuple[RunStatus, Ledger, Path]:
    lib = tmp_path / "library"
    run_dir = tmp_path / "run"
    (run_dir / "snapshot" / "agents").mkdir(parents=True)
    for ref in agents:
        shutil.copytree(
            lib / "agents" / ref.split("@")[0], run_dir / "snapshot" / "agents" / ref
        )
    ledger = Ledger.create(
        run_dir,
        run_id="r",
        pipeline="refine",
        node_ids=[n.id for n in pipeline.nodes],
        created_at="T0",
    )
    events = EventWriter(run_dir)
    runtime = MockRuntime(scenario)
    status = asyncio.run(
        run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=agents,
            registry=registry,
            runtime=runtime,
            ledger=ledger,
            events=events,
            clock=lambda: "T",
            sleeper=_no_sleep,
        )
    )
    return status, ledger, run_dir


def _verdict(v: str) -> str:
    return json.dumps({"verdict": v})


def test_revise_twice_then_approved(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=3, on_max_rounds="pass")
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r2": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body:r3": [ScriptedResponse(files={"doc.md": DOC3})],
            "refine.critic:r3": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert ledger.get_node("refine").status is NodeStatus.done
    assert (run_dir / "steps" / "refine" / "_out" / "doc.md").read_text("utf-8") == DOC3
    body_steps = [s for s in ledger.state.steps if s.startswith("refine.body:")]
    assert sorted(body_steps) == ["refine.body:r1", "refine.body:r2", "refine.body:r3"]


def test_approved_first_round(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=3, on_max_rounds="pass")
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert "refine.body:r2" not in ledger.state.steps
    assert (run_dir / "steps" / "refine" / "_out" / "doc.md").read_text("utf-8") == DOC1


def test_max_rounds_pass_takes_last_draft(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=2, on_max_rounds="pass")
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r2": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert ledger.get_node("refine").status is NodeStatus.done
    assert (run_dir / "steps" / "refine" / "_out" / "doc.md").read_text("utf-8") == DOC2
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert any(
        e["type"] == "log" and e["payload"].get("level") == "warning" for e in events
    )


def test_max_rounds_fail(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=2, on_max_rounds="fail")
    status, ledger, _ = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r2": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
        },
    )
    assert status is RunStatus.failed
    assert ledger.get_node("refine").status is NodeStatus.failed


def test_revision_materialization(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=2, on_max_rounds="pass")
    _, _, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r2": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    prev = run_dir / "steps" / "refine" / "body_r2" / "input" / "_previous" / "doc.md"
    verd = (
        run_dir / "steps" / "refine" / "body_r2" / "input" / "_verdict" / "verdict.json"
    )
    assert prev.read_text("utf-8") == DOC1
    assert json.loads(verd.read_text("utf-8"))["verdict"] == "revise"
    prompt = (run_dir / "steps" / "refine" / "body_r2" / "prompt.md").read_text("utf-8")
    assert "input/_previous/doc.md" in prompt


def test_invalid_verdict_fails_gate(tmp_path: Path) -> None:
    # A verdict that violates the verdict@v1 enum → critic gate fails → node failed.
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=3, on_max_rounds="pass", gate_retries=0)
    status, ledger, _ = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("maybe")})
            ],
        },
    )
    assert status is RunStatus.failed
    assert ledger.get_node("refine").status is NodeStatus.failed
    assert ledger.get_step("refine.critic:r1").status is StepStatus.failed


def test_resume_reuses_done_rounds_from_ledger(tmp_path: Path) -> None:
    # SPEC §10.5 / §18: round is derived from the ledger; resuming a loop whose
    # sub-steps are all done re-walks the rounds WITHOUT re-executing any step.
    _, agents, reg = _library(tmp_path)
    pl = _loop_pipeline(max_rounds=3, on_max_rounds="pass")
    scenario = {
        "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
        "refine.critic:r1": [
            ScriptedResponse(files={"verdict.json": _verdict("revise")})
        ],
        "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
        "refine.critic:r2": [
            ScriptedResponse(files={"verdict.json": _verdict("approved")})
        ],
    }
    status, ledger, run_dir = _run(tmp_path, pl, agents, reg, scenario)
    assert status is RunStatus.completed

    # simulate a resume: the node is re-scheduled but every sub-step is done.
    ledger.set_node_status("refine", NodeStatus.pending, error=None)
    ledger.save()
    reloaded = Ledger.load(run_dir)
    events = EventWriter(run_dir)
    status2 = asyncio.run(
        run_pipeline(
            run_dir,
            pipeline=pl,
            agents=agents,
            registry=reg,
            runtime=_BoomRuntime(),  # raises if any step re-executes
            ledger=reloaded,
            events=events,
            clock=lambda: "T",
            sleeper=_no_sleep,
        )
    )
    assert status2 is RunStatus.completed
    assert reloaded.get_node("refine").status is NodeStatus.done
    assert (run_dir / "steps" / "refine" / "_out" / "doc.md").read_text("utf-8") == DOC2


# --- body chain (SPEC §10.3) ------------------------------------------------


def _chain_pipeline(*, polisher_model: str | None = None) -> Pipeline:
    """A loop whose body is two elements: writer → polisher, then one critic."""
    second: dict = {"agent": "polisher@1", "inputs": {"draft": "@prev"}}
    if polisher_model is not None:
        second["model"] = polisher_model
    return Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "refine",
            "nodes": [
                {
                    "id": "refine",
                    "type": "loop",
                    "params": {"max_rounds": 3, "model": "kimi/kimi-k3"},
                    "body": [{"agent": "writer@1"}, second],
                    "critic": {"agent": "critic@1", "inputs": {"draft": "@body"}},
                    "outputs": {"doc": "@body"},
                }
            ],
        }
    )


def test_chain_runs_in_order_and_feeds_prev(tmp_path: Path) -> None:
    """Elements run in order; @prev carries the previous element's artifact."""
    _, agents, reg = _library(tmp_path)
    status, ledger, run_dir = _run(
        tmp_path,
        _chain_pipeline(),
        agents,
        reg,
        {
            "refine.body1:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.body2:r1": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert sorted(s for s in ledger.state.steps if s.startswith("refine.")) == [
        "refine.body1:r1",
        "refine.body2:r1",
        "refine.critic:r1",
    ]
    steps = run_dir / "steps" / "refine"
    # the polisher saw the writer's draft…
    assert (steps / "body2_r1" / "input" / "draft" / "draft.md").read_text(
        "utf-8"
    ) == DOC1
    # …the critic saw the polisher's (the round's draft is the LAST element)…
    assert (steps / "critic_r1" / "input" / "draft" / "draft.md").read_text(
        "utf-8"
    ) == DOC2
    # …and so does the node's assembled output
    assert (steps / "_out" / "doc.md").read_text("utf-8") == DOC2


def test_chain_revision_goes_to_the_first_element(tmp_path: Path) -> None:
    """On r≥2 the FIRST element revises, from the previous round's final draft."""
    _, agents, reg = _library(tmp_path)
    status, _, run_dir = _run(
        tmp_path,
        _chain_pipeline(),
        agents,
        reg,
        {
            "refine.body1:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.body2:r1": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.body1:r2": [ScriptedResponse(files={"doc.md": DOC3})],
            "refine.body2:r2": [ScriptedResponse(files={"doc.md": DOC3})],
            "refine.critic:r2": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    steps = run_dir / "steps" / "refine"
    prev = steps / "body1_r2" / "input" / "_previous" / "doc.md"
    assert prev.read_text("utf-8") == DOC2  # the round's draft, not the writer's
    assert (steps / "body1_r2" / "input" / "_verdict" / "verdict.json").exists()
    # the second element is not handed a revision context — it polishes what it gets
    assert not (steps / "body2_r2" / "input" / "_previous").exists()


def test_chain_element_keeps_its_own_model(tmp_path: Path) -> None:
    """Each element resolves its own model, keyed by its block name."""
    _, agents, reg = _library(tmp_path)
    pl = _chain_pipeline(polisher_model="openai/gpt-5.6")
    status, _, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body1:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.body2:r1": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    steps = run_dir / "steps" / "refine"
    assert "model=kimi/kimi-k3" in (steps / "body1_r1" / "raw.txt").read_text("utf-8")
    assert "model=openai/gpt-5.6" in (steps / "body2_r1" / "raw.txt").read_text("utf-8")


def test_single_element_body_keeps_historical_ids(tmp_path: Path) -> None:
    """A one-element chain must be indistinguishable from the old single block."""
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "refine",
            "nodes": [
                {
                    "id": "refine",
                    "type": "loop",
                    "params": {"max_rounds": 2, "model": "kimi/kimi-k3"},
                    "body": [{"agent": "writer@1"}],  # a LIST of one
                    "critic": {"agent": "critic@1", "inputs": {"draft": "@body"}},
                    "outputs": {"doc": "@body"},
                }
            ],
        }
    )
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert "refine.body:r1" in ledger.state.steps
    assert (run_dir / "steps" / "refine" / "body_r1").is_dir()


# --- gate_rules on a body/critic block actually run (SPEC §8/§5.1) ----------
#
# They did not, and nothing noticed. `_plan` was handed `block.params`, and
# `SubBlockParams` has no `gate_rules` field, so every declared rule resolved into
# `resolved.yaml` and then vanished at the last handoff. Measured on a live run: a writer
# whose node asked for 8 000-12 000 characters of prose was never told so — the bound is
# generated into the prompt from the same list — and produced 24 812, which the gate then
# passed because it was checking the artifact type's own rules alone.


def _gated_pipeline(
    *, body_rules: list | None = None, critic_rules: list | None = None
):
    body: dict = {"agent": "writer@1"}
    if body_rules is not None:
        body["gate_rules"] = body_rules
    critic: dict = {"agent": "critic@1", "inputs": {"draft": "@body"}}
    if critic_rules is not None:
        critic["gate_rules"] = critic_rules
    return Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "refine",
            "nodes": [
                {
                    "id": "refine",
                    "type": "loop",
                    "params": {
                        "max_rounds": 1,
                        "on_max_rounds": "pass",
                        "model": "kimi/kimi-k3",
                        "gate_retries": 0,
                    },
                    "body": body,
                    "critic": critic,
                    "outputs": {"doc": "@body"},
                }
            ],
        }
    )


def test_body_gate_rules_are_enforced(tmp_path: Path) -> None:
    """A body rule the draft violates must FAIL the step, not be ignored."""
    _, agents, reg = _library(tmp_path)
    pl = _gated_pipeline(body_rules=[{"rule": "min_length", "value": 5000}])
    status, ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],  # ~30 chars
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.failed
    report = json.loads(
        (run_dir / "steps" / "refine" / "body_r1" / "gate_report.json").read_text(
            "utf-8"
        )
    )
    assert report["ok"] is False
    assert any("min_length 5000" in p for p in report["ports"][0]["problems"])


def test_body_gate_rules_reach_the_prompt(tmp_path: Path) -> None:
    """The requirement is GENERATED into the prompt from the same list (I5).

    The writer being told the number is half of what the rule buys: without it the first
    draft is written blind and the gate can only reject it after it is paid for.
    """
    _, agents, reg = _library(tmp_path)
    pl = _gated_pipeline(body_rules=[{"rule": "min_length", "value": 5000}])
    _status, _ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    prompt = (run_dir / "steps" / "refine" / "body_r1" / "prompt.md").read_text("utf-8")
    assert "At least 5000 characters" in prompt


def test_body_gate_rules_measure_on_a_pass(tmp_path: Path) -> None:
    """A rule that runs records what it measured, pass or fail (SPEC §10.2)."""
    _, agents, reg = _library(tmp_path)
    pl = _gated_pipeline(body_rules=[{"rule": "min_length", "value": 10}])
    status, _ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.completed
    report = json.loads(
        (run_dir / "steps" / "refine" / "body_r1" / "gate_report.json").read_text(
            "utf-8"
        )
    )
    assert report["ports"][0]["measures"]["min_length"] == 10


def test_critic_gate_rules_are_enforced(tmp_path: Path) -> None:
    """`loop.critic` carries the field too (SPEC-DSL §5.1), so it must run there."""
    _, agents, reg = _library(tmp_path)
    pl = _gated_pipeline(critic_rules=[{"rule": "min_length", "value": 5000}])
    status, _ledger, run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.failed
    report = json.loads(
        (run_dir / "steps" / "refine" / "critic_r1" / "gate_report.json").read_text(
            "utf-8"
        )
    )
    assert report["ok"] is False


def test_block_params_still_apply(tmp_path: Path) -> None:
    """The fix moved `block` from its params to the block; params must still work."""
    _, agents, reg = _library(tmp_path)
    pl = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "refine",
            "nodes": [
                {
                    "id": "refine",
                    "type": "loop",
                    "params": {
                        "max_rounds": 1,
                        "on_max_rounds": "pass",
                        "model": "kimi/kimi-k3",
                        "gate_retries": 5,
                    },
                    # the BLOCK overrides the loop: no retries here
                    "body": {
                        "agent": "writer@1",
                        "params": {"gate_retries": 0},
                        "gate_rules": [{"rule": "min_length", "value": 5000}],
                    },
                    "critic": {"agent": "critic@1", "inputs": {"draft": "@body"}},
                    "outputs": {"doc": "@body"},
                }
            ],
        }
    )
    status, ledger, _run_dir = _run(
        tmp_path,
        pl,
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert status is RunStatus.failed
    # one attempt, not six: the block's gate_retries=0 beat the loop's 5
    assert ledger.state.steps["refine.body:r1"].tries == 1


# --- what the loop leaves open (SPEC §10.3) ---------------------------------
#
# A loop that hits its ceiling warned "max_rounds reached; passing" and handed the draft on,
# and everything the critic still objected to lived in `critic_r<n>/output/verdict.json` — a
# path nobody opens who is not already debugging. Measured across two live runs of one
# article: neither was ever approved, the remarks that shipped were real, and the only trace
# was one warning line in the event log.


def _verdict_with(v: str, issues: list[dict]) -> str:
    return json.dumps({"verdict": v, "issues": issues})


def _unresolved(run_dir: Path) -> Path:
    return run_dir / "steps" / "refine" / "_out" / "unresolved.md"


def test_open_items_are_written_when_rounds_run_out(tmp_path: Path) -> None:
    _, agents, reg = _library(tmp_path)
    status, ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=1, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(
                    files={
                        "verdict.json": _verdict_with(
                            "revise",
                            [
                                {"section": "Многоголовость", "note": "формы неверны"},
                                {"note": "термин без введения"},
                            ],
                        )
                    }
                )
            ],
        },
    )
    assert status is RunStatus.completed
    assert ledger.state.nodes["refine"].status is NodeStatus.done
    text = _unresolved(run_dir).read_text("utf-8")
    assert "исчерпаны" in text
    assert "1. [Многоголовость] формы неверны" in text
    assert "2. термин без введения" in text


def test_approved_with_remarks_still_reports_them(tmp_path: Path) -> None:
    """"Publishable, but this is wrong" is a finding; silence reads as approval."""
    _, agents, reg = _library(tmp_path)
    _status, _ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=2, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(
                    files={
                        "verdict.json": _verdict_with(
                            "approved", [{"note": "мелочь, но неверно"}]
                        )
                    }
                )
            ],
        },
    )
    text = _unresolved(run_dir).read_text("utf-8")
    assert "одобрил" in text
    assert "мелочь, но неверно" in text


def test_a_clean_approval_writes_no_file(tmp_path: Path) -> None:
    """Nothing open means nothing to report — an empty report is noise."""
    _, agents, reg = _library(tmp_path)
    _status, _ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=2, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("approved")})
            ],
        },
    )
    assert not _unresolved(run_dir).exists()


def test_the_report_names_the_round_it_came_from(tmp_path: Path) -> None:
    """The chosen round, not the last one attempted: they differ after an approval."""
    _, agents, reg = _library(tmp_path)
    _status, _ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=3, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.body:r2": [ScriptedResponse(files={"doc.md": DOC2})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
            "refine.critic:r2": [
                ScriptedResponse(
                    files={
                        "verdict.json": _verdict_with("approved", [{"note": "остаток"}])
                    }
                )
            ],
        },
    )
    text = _unresolved(run_dir).read_text("utf-8")
    assert "круг 2 из 3" in text


def test_the_open_items_are_announced_in_the_log(tmp_path: Path) -> None:
    """A file nobody is told about is a file nobody reads."""
    _, agents, reg = _library(tmp_path)
    _status, _ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=1, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(
                    files={"verdict.json": _verdict_with("revise", [{"note": "раз"}])}
                )
            ],
        },
    )
    events = (run_dir / "events.jsonl").read_text("utf-8")
    assert "unresolved.md" in events
    assert "1 open item(s)" in events


def test_a_verdict_without_issues_is_not_a_crash(tmp_path: Path) -> None:
    """`issues` is optional in verdict@v1, so its absence must read as "nothing open"."""
    _, agents, reg = _library(tmp_path)
    status, _ledger, run_dir = _run(
        tmp_path,
        _loop_pipeline(max_rounds=1, on_max_rounds="pass"),
        agents,
        reg,
        {
            "refine.body:r1": [ScriptedResponse(files={"doc.md": DOC1})],
            "refine.critic:r1": [
                ScriptedResponse(files={"verdict.json": _verdict("revise")})
            ],
        },
    )
    assert status is RunStatus.completed
    assert not _unresolved(run_dir).exists()
