"""Tests for the single step lifecycle (SPEC §10.2, §9, §10.1, §10.4, §11)."""

from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path

import pytest

from refract.models.agent import AgentSpec
from refract.models.ledger import StepOutcome, StepStatus
from refract.models.types import ItemInfo, MinLengthRule
from refract.registry import ArtifactRegistry
from refract.runtime.base import EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.state import Ledger
from refract.steps import (
    AgentStepPlan,
    CollectionInput,
    DirAnyInput,
    FileInput,
    MapItemInput,
    execute_agent_step,
)

# --- fixtures / builders -----------------------------------------------------


def _write_registry(tmp_path: Path) -> ArtifactRegistry:
    types_dir = tmp_path / "lib" / "types"
    types_dir.mkdir(parents=True, exist_ok=True)
    (types_dir / "artifact_types.yaml").write_text(
        """
version: "0.1"
types:
  source@v1:   { kind: any }
  extract@v1:  { kind: file, format: json, schema: extract.schema.json }
  requirements@v1:
    kind: file
    format: markdown
    rules:
      - { rule: min_length, value: 20 }
""",
        encoding="utf-8",
    )
    schema_dir = types_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "extract.schema.json").write_text(
        json.dumps({"type": "object", "required": ["value"]}), encoding="utf-8"
    )
    return ArtifactRegistry.load(tmp_path / "lib")


def _agent(**kwargs: object) -> AgentSpec:
    base: dict[str, object] = {
        "name": "writer",
        "version": 1,
        "consumes": [],
        "produces": [{"port": "doc", "type": "requirements@v1"}],
    }
    base.update(kwargs)
    return AgentSpec.model_validate(base)


def _plan(
    tmp_path: Path,
    registry: ArtifactRegistry,
    agent: AgentSpec,
    *,
    step_id: str = "write:main",
    node_id: str = "write",
    inputs: list | None = None,
    gate_retries: int = 2,
    infra_retries: int = 2,
    timeout_s: int = 3600,
    agent_prompt: str = "You are a writer agent.",
    gate_rules: list | None = None,
) -> AgentStepPlan:
    workdir = tmp_path / "steps" / node_id / "main"
    workdir.mkdir(parents=True, exist_ok=True)
    agent_dir = tmp_path / "agent_pkg"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text(agent_prompt, encoding="utf-8")
    return AgentStepPlan(
        step_id=step_id,
        node_id=node_id,
        workdir=workdir,
        agent=agent,
        agent_dir=agent_dir,
        model="mock/mock-1",
        registry=registry,
        inputs=inputs or [],
        gate_retries=gate_retries,
        infra_retries=infra_retries,
        timeout_s=timeout_s,
        gate_rules=gate_rules or [],
    )


def _ledger(tmp_path: Path, node_ids: list[str]) -> Ledger:
    return Ledger.create(
        tmp_path / "run",
        run_id="run_test",
        pipeline="p",
        node_ids=node_ids,
        created_at="t0",
    )


def _clock_seq() -> "callable":
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def registry(tmp_path: Path) -> ArtifactRegistry:
    return _write_registry(tmp_path)


# --- 1. input materialization -------------------------------------------------


class TestInputMaterialization:
    async def test_file_input_lands_at_port_dot_ext(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        # SPEC §10.1
        rtype = registry.get("extract@v1")
        assert rtype is not None
        src = tmp_path / "src" / "extract.json"
        src.parent.mkdir(parents=True)
        src.write_text(json.dumps({"value": 1}), encoding="utf-8")

        agent = _agent(
            consumes=[{"port": "extracted", "type": "extract@v1"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        )
        plan = _plan(
            tmp_path,
            registry,
            agent,
            inputs=[FileInput(port="extracted", src=src, rtype=rtype)],
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(
                        files={"doc.md": "x" * 30 + "\nRequirements body here."}
                    )
                ]
            }
        )
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        dst = plan.workdir / "input" / "extracted" / "extracted.json"
        assert dst.exists()
        assert json.loads(dst.read_text("utf-8")) == {"value": 1}

    async def test_dir_any_input_flattened(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        # SPEC §10.1: dir source -> contents placed directly in port dir
        src_dir = tmp_path / "src_dir"
        src_dir.mkdir()
        (src_dir / "a.txt").write_text("A", encoding="utf-8")

        agent = _agent(consumes=[{"port": "source", "type": "source@v1"}])
        plan = _plan(
            tmp_path,
            registry,
            agent,
            inputs=[DirAnyInput(port="source", src=src_dir)],
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "y" * 30})]})
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        port_dir = plan.workdir / "input" / "source"
        assert (port_dir / "a.txt").read_text("utf-8") == "A"
        assert not (port_dir / "src_dir").exists()

    async def test_dir_any_input_file_source_under_own_name(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        src_file = tmp_path / "rfp.pdf"
        src_file.write_text("payload", encoding="utf-8")
        agent = _agent(consumes=[{"port": "source", "type": "source@v1"}])
        plan = _plan(
            tmp_path,
            registry,
            agent,
            inputs=[DirAnyInput(port="source", src=src_file)],
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "z" * 30})]})
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)
        assert (plan.workdir / "input" / "source" / "rfp.pdf").read_text(
            "utf-8"
        ) == "payload"

    async def test_collection_input_writes_manifest_and_items(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        src_coll = tmp_path / "src_collection"
        src_coll.mkdir()
        (src_coll / "_collection.json").write_text(
            json.dumps(
                {
                    "type": "collection<extract@v1>",
                    "items": [
                        {
                            "slug": "item-1",
                            "source": "a.json",
                            "source_hash": "sha256:abc",
                            "status": "ok",
                            "path": "item-1/",
                            "error": None,
                        }
                    ],
                    "stats": {"total": 1, "ok": 1, "failed": 0},
                }
            ),
            encoding="utf-8",
        )
        item_dir = src_coll / "item-1"
        item_dir.mkdir()
        (item_dir / "extract.json").write_text(
            json.dumps({"value": 1}), encoding="utf-8"
        )

        agent = _agent(
            consumes=[{"port": "extracts", "type": "collection<extract@v1>"}]
        )
        plan = _plan(
            tmp_path,
            registry,
            agent,
            inputs=[CollectionInput(port="extracts", src=src_coll)],
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "w" * 30})]})
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        port_dir = plan.workdir / "input" / "extracts"
        assert (port_dir / "_collection.json").exists()
        assert (port_dir / "item-1" / "extract.json").exists()

    async def test_map_item_input_writes_payload_and_item_json(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        payload = tmp_path / "rfp.pdf"
        payload.write_text("payload", encoding="utf-8")
        item = ItemInfo(slug="rfp-doc", source="rfp.pdf", source_hash="sha256:abc")

        agent = _agent(consumes=[{"port": "source", "type": "source@v1"}])
        plan = _plan(
            tmp_path,
            registry,
            agent,
            inputs=[MapItemInput(port="source", src=payload, item=item)],
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "v" * 30})]})
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        port_dir = plan.workdir / "input" / "source"
        assert (port_dir / "rfp.pdf").read_text("utf-8") == "payload"
        round_tripped = ItemInfo.model_validate_json(
            (port_dir / "_item.json").read_text("utf-8")
        )
        assert round_tripped == item


# --- 2. prompt.md composition -------------------------------------------------


class TestPromptComposition:
    async def test_system_prompt_is_prefix_of_workdir_prompt(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        # SPEC §11 item 1: agent's prompt.md is the system part (prefix)
        agent = _agent()
        plan = _plan(
            tmp_path,
            registry,
            agent,
            agent_prompt="SYSTEM PROMPT MARKER\nBe concise.",
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "a" * 30})]})
        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        full_prompt = (plan.workdir / "prompt.md").read_text("utf-8")
        assert full_prompt.startswith("SYSTEM PROMPT MARKER\nBe concise.")


# --- 3. happy path -------------------------------------------------------------


class TestHappyPath:
    async def test_completed_and_gate_passes(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "b" * 30})]})
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.done
        assert state.outcome is StepOutcome.ok
        assert state.tries == 1

        record = ledger.get_step(plan.step_id)
        assert record is not None
        assert record.status is StepStatus.done
        assert record.outcome is StepOutcome.ok
        assert record.tries == 1

        assert (plan.workdir / "raw.txt").exists()
        assert (plan.workdir / "agent.events.jsonl").exists()


# --- 4. gate fail -> retry with feedback -> success ---------------------------


class TestGateRetrySuccess:
    async def test_fail_then_pass(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, gate_retries=2)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(files={"doc.md": "too short"}),  # fails min_length
                    ScriptedResponse(files={"doc.md": "c" * 30}),  # passes
                ]
            }
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.done
        assert state.outcome is StepOutcome.ok
        assert state.tries == 2

        attempt1 = plan.workdir / "attempts" / "1"
        assert (attempt1 / "prompt.md").exists()
        assert (attempt1 / "gate_report.json").exists()
        assert (attempt1 / "output" / "doc.md").exists()

        # 2nd attempt's current prompt.md must contain the gate feedback
        current_prompt = (plan.workdir / "prompt.md").read_text("utf-8")
        assert "Validation feedback" in current_prompt or "min_length" in current_prompt


class TestNodeGateRules:
    """SPEC §8 ``gate_rules``: a project states its own terms without touching the type."""

    def _rule(self, value: int) -> MinLengthRule:
        return MinLengthRule(rule="min_length", value=value)

    async def test_node_rule_tightens_the_type(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """Output satisfies the type (min_length 20) but not the node's own floor."""
        agent = _agent()
        plan = _plan(
            tmp_path, registry, agent, gate_retries=1, gate_rules=[self._rule(500)]
        )
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(files={"doc.md": "c" * 30}),  # passes the type
                    ScriptedResponse(files={"doc.md": "c" * 500}),  # meets the node
                ]
            }
        )

        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.done
        assert state.tries == 2
        report = json.loads(
            (plan.workdir / "attempts" / "1" / "gate_report.json").read_text("utf-8")
        )
        assert any("500" in p for p in report["ports"][0]["problems"])

    async def test_the_agent_is_told_the_tightened_number(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """I5: what the step is held to is generated into the prompt, not hand-written."""
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, gate_rules=[self._rule(500)])
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "c" * 500})]})

        await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        prompt = (plan.workdir / "prompt.md").read_text("utf-8")
        assert "500 characters" in prompt

    async def test_without_node_rules_only_the_type_applies(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "c" * 30})]})

        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.outcome is StepOutcome.ok
        assert state.tries == 1


# --- 5. gate exhausted ---------------------------------------------------------


class TestGateExhausted:
    async def test_all_fail_gate_exhausted(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, gate_retries=2)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": "short"})]}  # always fails
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.failed
        assert state.outcome is StepOutcome.failed_validation
        assert state.tries == 3

        assert (plan.workdir / "attempts" / "1").exists()
        assert (plan.workdir / "attempts" / "2").exists()
        # 3rd (final) attempt stays current — never archived (guards the off-by-one)
        assert not (plan.workdir / "attempts" / "3").exists()
        assert (plan.workdir / "gate_report.json").exists()


# --- 6. infra retries are a separate counter -----------------------------------


class TestInfraRetries:
    async def test_infra_error_then_success_no_gate_try_consumed(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, infra_retries=2)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(completed=False),  # infra error
                    ScriptedResponse(files={"doc.md": "d" * 30}),  # success
                ]
            }
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.done
        assert state.outcome is StepOutcome.ok
        # infra retry did not consume a gate try
        assert state.tries == 1
        assert not (plan.workdir / "attempts").exists()

    async def test_infra_retries_exhausted(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, infra_retries=1)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(completed=False)]})
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.failed
        assert state.outcome is StepOutcome.failed_infra

    async def test_the_adapters_reason_reaches_the_ledger(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """An adapter may explain a retryable failure; "exhausted" alone misleads.

        The Claude Code adapter routes a transient provider error (a rate limit,
        through the infra retries WITH its summary attached, so a run that died on the
        provider does not read like an engine bug.
        """
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, infra_retries=1)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(
                        completed=False, agent_error="APIError 429: slow down"
                    )
                ]
            }
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.outcome is StepOutcome.failed_infra
        assert state.error == "APIError 429: slow down"


# --- 7. timeout -----------------------------------------------------------------


class _SleepyRuntime:
    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        await asyncio.sleep(5)
        return StepResult(completed=True)  # pragma: no cover - never reached

    async def close(self) -> None:
        return None


class TestTimeout:
    async def test_timeout_marks_step_timeout(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent, timeout_s=0.01)
        ledger = _ledger(tmp_path, ["write"])
        state = await execute_agent_step(
            plan, _SleepyRuntime(), ledger, sleeper=_no_sleep
        )
        assert state.status is StepStatus.failed
        assert state.outcome is StepOutcome.timeout


# --- 8. agent_error --------------------------------------------------------------


class TestAgentError:
    async def test_agent_error_marks_failed_agent(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(agent_error="boom")]})
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.failed
        assert state.outcome is StepOutcome.failed_agent
        assert state.error == "boom"
        assert not (plan.workdir / "output" / "doc.md").exists()


# --- 9. HITL question artifact ---------------------------------------------------


class TestHITL:
    def _agent_with_question(self) -> AgentSpec:
        return _agent(
            produces=[
                {"port": "doc", "type": "requirements@v1"},
                {"port": "clarification", "type": "question@v1", "optional": True},
            ]
        )

    async def test_valid_question_pauses_for_human(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        # SPEC §16.9 (phase 3): a valid question@v1 parks the step waiting_human,
        # persists the question, and emits a `question` event.
        agent = self._agent_with_question()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        events: list[dict] = []
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(
                        files={
                            "clarification.json": json.dumps({"question": "which db?"})
                        }
                    )
                ]
            }
        )
        state = await execute_agent_step(
            plan, runtime, ledger, on_event=events.append, sleeper=_no_sleep
        )

        assert state.status is StepStatus.waiting_human
        assert state.outcome is None
        assert (plan.workdir / "hitl" / "question.json").exists()
        assert any(e["type"] == "question" for e in events)

    async def test_answer_lets_the_step_proceed(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        # After a human answer lands at hitl/answer.json, re-running the step folds
        # the answer into the prompt and the agent produces the real output.
        agent = self._agent_with_question()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        # turn 1: the agent asks a question -> waiting_human
        asking = MockRuntime(
            {
                "*": [
                    ScriptedResponse(
                        files={"clarification.json": json.dumps({"question": "?"})}
                    )
                ]
            }
        )
        s1 = await execute_agent_step(plan, asking, ledger, sleeper=_no_sleep)
        assert s1.status is StepStatus.waiting_human

        # human answers, then the step is re-run
        (plan.workdir / "hitl" / "answer.json").write_text(
            json.dumps({"answer": "use Postgres"}), encoding="utf-8"
        )
        answering = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": "# ok\n" + "z" * 40})]}
        )
        s2 = await execute_agent_step(plan, answering, ledger, sleeper=_no_sleep)
        assert s2.status is StepStatus.done
        assert s2.outcome is StepOutcome.ok
        # the answer was consumed and the prior (question) attempt archived
        assert not (plan.workdir / "hitl" / "answer.json").exists()
        assert (plan.workdir / "attempts" / "1").is_dir()

    async def test_without_question_file_passes(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = self._agent_with_question()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "f" * 30})]})
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.status is StepStatus.done
        assert state.outcome is StepOutcome.ok


# --- 10. ledger integration: running -> terminal, timestamps, events -------------


class TestLedgerIntegration:
    async def test_running_then_terminal_with_timestamps_and_events(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        agent = _agent()
        plan = _plan(tmp_path, registry, agent)
        ledger = _ledger(tmp_path, ["write"])
        events: list[dict] = []
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": "g" * 30})]})

        clock = _clock_seq()
        state = await execute_agent_step(
            plan,
            runtime,
            ledger,
            on_event=events.append,
            clock=clock,
            sleeper=_no_sleep,
        )

        assert state.started_at is not None
        assert state.finished_at is not None
        assert state.started_at != state.finished_at

        step_events = [e for e in events if e["type"] == "step_state_changed"]
        assert step_events[0]["payload"]["to"] == "running"
        assert step_events[-1]["payload"]["to"] == "done"
        assert step_events[-1]["payload"]["outcome"] == "ok"


def test_retrying_a_step_rebuilds_its_inputs(tmp_path: Path) -> None:
    """A second materialization pass must not trip over the first (SPEC §10.1).

    ``link_or_copy`` requires a destination that does not exist, so re-running a step
    whose ``input/`` was already laid out raised ``FileExistsError`` — every
    ``resume --retry-failed`` of a non-map step died on its own leftovers. A live run
    hit this resuming the analysis step after the subscription window ran out.
    """
    from refract.steps import AuxFileInput, _materialize

    src = tmp_path / "src"
    (src / "notes").mkdir(parents=True)
    (src / "notes" / "a.json").write_text('{"source": "a"}', encoding="utf-8")
    input_root = tmp_path / "work" / "input"
    input_root.mkdir(parents=True)

    inputs = [AuxFileInput(src=src / "notes" / "a.json", rel_path="notes/a.json")]
    _materialize(inputs, input_root)
    first = (input_root / "notes" / "a.json").read_text(encoding="utf-8")

    # the retry: same inputs, workdir already populated
    _materialize(inputs, input_root)
    assert (input_root / "notes" / "a.json").read_text(encoding="utf-8") == first

    # stale material from an earlier attempt does not survive the rebuild
    (input_root / "leftover").mkdir()
    (input_root / "leftover" / "old.txt").write_text("stale", encoding="utf-8")
    _materialize(inputs, input_root)
    assert not (input_root / "leftover").exists()


# --- 11. usage accounting (SPEC §9) -----------------------------------------

_LONG = "x" * 30 + "\nRequirements body here."
_SHORT = "too short"


def _usage(cost: float, *, tokens_in: int = 100, tokens_out: int = 20) -> dict:
    """A usage report shaped like the CLI's ``result`` frame (SPEC §12)."""
    return {
        "cost": cost,
        "tokens": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 3,
        },
        "duration_ms": 1500,
    }


class TestUsageAccounting:
    """The adapter measured cost all along; until this landed nobody read it."""

    async def test_single_call_lands_in_the_ledger(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = _plan(tmp_path, registry, _agent())
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": _LONG}, usage=_usage(0.25))]}
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.usage is not None
        assert state.usage.cost_usd == pytest.approx(0.25)
        assert state.usage.calls == 1
        assert state.usage.input_tokens == 100
        assert state.usage.cache_read_tokens == 7
        assert state.usage.cache_write_tokens == 3
        assert state.usage.duration_ms == 1500
        assert ledger.total_usage().cost_usd == pytest.approx(0.25)
        assert ledger.usage_by_node()["write"].cost_usd == pytest.approx(0.25)

    async def test_gate_retries_accumulate_what_was_paid(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """A budget cares about what was PAID, not what the surviving attempt cost."""
        plan = _plan(tmp_path, registry, _agent(), gate_retries=2)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(files={"doc.md": _SHORT}, usage=_usage(0.10)),
                    ScriptedResponse(files={"doc.md": _LONG}, usage=_usage(0.20)),
                ]
            }
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.outcome is StepOutcome.ok
        assert state.usage is not None
        assert state.usage.calls == 2
        assert state.usage.cost_usd == pytest.approx(0.30)

    async def test_one_event_per_paid_call_carries_its_ordinal(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """A run's cost must be reconstructible from events alone (I7)."""
        plan = _plan(tmp_path, registry, _agent(), gate_retries=1)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(files={"doc.md": _SHORT}, usage=_usage(0.10)),
                    ScriptedResponse(files={"doc.md": _LONG}, usage=_usage(0.20)),
                ]
            }
        )
        events: list[dict] = []
        await execute_agent_step(
            plan, runtime, ledger, on_event=events.append, sleeper=_no_sleep
        )

        usage_events = [e for e in events if e["type"] == "usage"]
        assert [e["payload"]["call"] for e in usage_events] == [1, 2]
        assert [e["payload"]["cost_usd"] for e in usage_events] == [0.10, 0.20]
        # the per-call event is the CALL's cost, not the running total
        assert "calls" not in usage_events[0]["payload"]

    async def test_infra_retry_is_paid_for_and_visible(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """A step that retried used to look exactly like one that ran once."""
        plan = _plan(tmp_path, registry, _agent(), infra_retries=2)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(
                        completed=False,
                        agent_error="You've hit your session limit",
                        usage=_usage(0.05),
                    ),
                    ScriptedResponse(files={"doc.md": _LONG}, usage=_usage(0.20)),
                ]
            }
        )
        events: list[dict] = []
        state = await execute_agent_step(
            plan, runtime, ledger, on_event=events.append, sleeper=_no_sleep
        )

        assert state.outcome is StepOutcome.ok
        assert state.usage is not None
        # the failed call burned tokens before the limit answered: it is still paid work
        assert state.usage.cost_usd == pytest.approx(0.25)
        assert state.usage.calls == 2
        retries = [e for e in events if e.get("payload", {}).get("infra_retry")]
        assert len(retries) == 1
        assert retries[0]["payload"]["infra_retry"]["attempt"] == 1
        assert "session limit" in retries[0]["payload"]["infra_retry"]["reason"]

    async def test_failed_step_still_records_what_it_spent(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = _plan(tmp_path, registry, _agent(), gate_retries=0)
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": _SHORT}, usage=_usage(0.4))]}
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.outcome is StepOutcome.failed_validation
        assert state.usage is not None
        assert state.usage.cost_usd == pytest.approx(0.4)

    async def test_runtime_that_reports_nothing_leaves_usage_absent(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """Usage is optional in the runtime contract (SPEC §12) — MockRuntime's default."""
        plan = _plan(tmp_path, registry, _agent())
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": _LONG})]})
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.usage is None
        assert ledger.total_usage().calls == 0

    async def test_empty_report_still_counts_the_call(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = _plan(tmp_path, registry, _agent())
        ledger = _ledger(tmp_path, ["write"])
        runtime = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": _LONG}, usage={})]}
        )
        state = await execute_agent_step(plan, runtime, ledger, sleeper=_no_sleep)

        assert state.usage is not None
        assert state.usage.calls == 1
        assert state.usage.cost_usd == 0.0


# --- a gate retry edits the rejected attempt, it does not start over ----------
#
# Measured on a live article: three attempts at one writing step cost $11.00, of which
# $9.17 was spent re-composing eleven thousand characters that were already almost right.
# Archiving moves `output/` away, so the agent had nothing to edit — while the feedback was
# worded as an edit ("this revision must end shorter than it started").

SHORT_DOC = "# Doc\n\n" + "short " * 5
LONG_DOC = "# Doc\n\n" + "long " * 200


class TestRetryEditsRatherThanRewrites:
    def _run(
        self, tmp_path: Path, registry: ArtifactRegistry, files: list[dict]
    ) -> AgentStepPlan:
        plan = _plan(
            tmp_path,
            registry,
            _agent(),
            gate_rules=[MinLengthRule(rule="min_length", value=500)],
        )
        runtime = MockRuntime({"*": [ScriptedResponse(files=f) for f in files]})
        asyncio.run(
            execute_agent_step(
                plan, runtime, _ledger(tmp_path, ["write"]), sleeper=_no_sleep
            )
        )
        return plan

    def test_the_rejected_attempt_comes_back_as_an_input(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = self._run(
            tmp_path, registry, [{"doc.md": SHORT_DOC}, {"doc.md": LONG_DOC}]
        )
        prompt = (plan.workdir / "prompt.md").read_text("utf-8")
        assert "input/_rejected" in prompt
        assert "Start from it" in prompt
        landed = plan.workdir / "input" / "_rejected" / "doc.md"
        assert landed.read_text("utf-8") == SHORT_DOC

    def test_the_archive_still_holds_what_was_rejected(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """The linked copy is for editing; the archive stays the record (SPEC §10.2)."""
        plan = self._run(
            tmp_path, registry, [{"doc.md": SHORT_DOC}, {"doc.md": LONG_DOC}]
        )
        archived = plan.workdir / "attempts" / "1" / "output" / "doc.md"
        assert archived.read_text("utf-8") == SHORT_DOC

    def test_the_first_attempt_has_no_rejected_input(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """Nothing was rejected yet, so nothing must be offered as one."""
        plan = self._run(tmp_path, registry, [{"doc.md": LONG_DOC}])
        assert not (plan.workdir / "input" / "_rejected").exists()
        assert "input/_rejected" not in (plan.workdir / "prompt.md").read_text("utf-8")

    def test_an_attempt_that_produced_nothing_is_not_offered_back(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """An empty output is not a draft to edit; offering it is worse than nothing."""
        plan = self._run(tmp_path, registry, [{}, {"doc.md": LONG_DOC}])
        assert not (plan.workdir / "input" / "_rejected").exists()


class TestDeclaredEnvReachesTheSubprocess:
    """The positive path, end to end.

    Its absence is exactly how a blocker shipped: `env` was added to the contract, honoured
    by `step_env`, and never put on the `StepSpec`, so `plan.agent.env` was read nowhere.
    The existing tests only asserted the negative — that undeclared secrets do NOT leak —
    which a permanently empty list satisfies perfectly.
    """

    def test_a_declared_variable_is_on_the_spec_and_survives_step_env(
        self, tmp_path: Path, registry: ArtifactRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from refract.models.config import McpFile
        from refract.runtime.claude_code import step_env

        monkeypatch.setenv("REFRACT_TEST_TOOL_BIN", "C:/tool/bin.exe")
        monkeypatch.setenv("REFRACT_TEST_UNDECLARED", "secret")

        agent = _agent()
        agent = agent.model_copy(update={"env": ["REFRACT_TEST_TOOL_BIN"]})
        plan = _plan(tmp_path, registry, agent)
        seen: dict[str, list[str]] = {}

        class _Capture:
            async def run_step(self, spec, on_event):  # noqa: ANN001
                seen["env"] = list(spec.env)
                (spec.workdir / "output").mkdir(parents=True, exist_ok=True)
                (spec.workdir / "output" / "doc.md").write_text(
                    "# Doc\n\n" + "x" * 100, encoding="utf-8"
                )
                return StepResult(completed=True)

            async def close(self) -> None:
                return None

        asyncio.run(
            execute_agent_step(plan, _Capture(), _ledger(tmp_path, ["write"]), sleeper=_no_sleep)
        )
        # the contract's declaration reached the runtime...
        assert seen["env"] == ["REFRACT_TEST_TOOL_BIN"]
        # ...and the runtime's allow-list lets it through while still holding the line
        env = step_env(dict(os.environ), needs=agent.needs, mcp=McpFile(), declared=seen["env"])
        assert env["REFRACT_TEST_TOOL_BIN"] == "C:/tool/bin.exe"
        assert "REFRACT_TEST_UNDECLARED" not in env


class TestRejectedIsACopyNotALink:
    """The archive is the one thing in a step that must not change (I2).

    `link_or_copy` prefers a symlink, and a symlink here points into
    `attempts/<n>/output/` — inside the workdir, so the guard permits writing through it.
    An agent that edits in place instead of copying would rewrite the record of what the
    gate rejected while leaving `output/` empty.
    """

    def _twice(self, tmp_path: Path, registry: ArtifactRegistry) -> AgentStepPlan:
        plan = _plan(
            tmp_path,
            registry,
            _agent(),
            gate_rules=[MinLengthRule(rule="min_length", value=500)],
        )
        runtime = MockRuntime(
            {
                "*": [
                    ScriptedResponse(files={"doc.md": "# Doc\n\n" + "short " * 5}),
                    ScriptedResponse(files={"doc.md": "# Doc\n\n" + "long " * 200}),
                ]
            }
        )
        asyncio.run(
            execute_agent_step(plan, runtime, _ledger(tmp_path, ["write"]), sleeper=_no_sleep)
        )
        return plan

    def test_the_rejected_input_is_not_a_symlink(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = self._twice(tmp_path, registry)
        landed = plan.workdir / "input" / "_rejected" / "doc.md"
        assert landed.exists()
        assert not landed.is_symlink()

    def test_editing_the_rejected_input_cannot_touch_the_archive(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        plan = self._twice(tmp_path, registry)
        landed = plan.workdir / "input" / "_rejected" / "doc.md"
        archived = plan.workdir / "attempts" / "1" / "output" / "doc.md"
        before = archived.read_text("utf-8")
        landed.write_text("an agent edited this in place", encoding="utf-8")
        assert archived.read_text("utf-8") == before

    def test_the_archive_records_what_that_attempt_was_handed(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        """I9: attempt 3's `prompt.md` names a directory attempt 4 overwrites."""
        plan = self._twice(tmp_path, registry)
        # attempt 1 had no rejected input, so no manifest; a later one would
        assert not (plan.workdir / "attempts" / "1" / "rejected_inputs.txt").exists()


class TestRetryContextSurvivesResume:
    """A resumed step re-enters with `tries` at 0 and both locals gone, and `_materialize`
    wipes `input/`. Without rebuilding, the agent writes the artifact again from its
    sources — the very rewrite this mechanism exists to prevent, on the runs that cost
    most."""

    def test_the_rejected_document_and_feedback_are_rebuilt(
        self, tmp_path: Path, registry: ArtifactRegistry
    ) -> None:
        rules = [MinLengthRule(rule="min_length", value=500)]
        short = "# Doc\n\n" + "short " * 5

        # first run: one failed attempt, then the step dies on infra so nothing is done
        plan = _plan(tmp_path, registry, _agent(), gate_rules=rules, gate_retries=0)
        runtime = MockRuntime({"*": [ScriptedResponse(files={"doc.md": short})]})
        asyncio.run(
            execute_agent_step(plan, runtime, _ledger(tmp_path, ["write"]), sleeper=_no_sleep)
        )
        # gate_retries=0, so the attempt is NOT archived: archiving happens at the start
        # of the next iteration, and there was none. It sits in the workdir root, which is
        # exactly the shape an interrupted retry leaves behind.
        assert (plan.workdir / "output" / "doc.md").read_text("utf-8") == short
        assert (plan.workdir / "gate_report.json").exists()

        # resume: same workdir, fresh call, and the retry context comes off disk
        plan2 = _plan(tmp_path, registry, _agent(), gate_rules=rules)
        runtime2 = MockRuntime(
            {"*": [ScriptedResponse(files={"doc.md": "# Doc\n\n" + "long " * 200})]}
        )
        asyncio.run(
            execute_agent_step(plan2, runtime2, _ledger(tmp_path, ["write2"]), sleeper=_no_sleep)
        )
        prompt = (plan2.workdir / "prompt.md").read_text("utf-8")
        assert "input/_rejected" in prompt, "the resumed step started from scratch"
        assert "min_length 500" in prompt, "the feedback was not rebuilt from the archive"
        assert (plan2.workdir / "input" / "_rejected" / "doc.md").read_text("utf-8") == short
