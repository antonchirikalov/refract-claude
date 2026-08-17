"""Tests for map-node fan-out (SPEC §10.3): one step per ``ok`` collection item,
reassembled into an output collection under ``steps/<node>/_out/<port>/``.

A tiny ``builtin/seed`` node (registered only for this module, via monkeypatch)
stands in for a real producer: it writes a ``collection<source@v1>`` manifest
with exactly the items/payloads/statuses the test wants, including pre-seeded
``failed`` input items — something a real scanner can't do portably. No
network, no real CLI: agent behavior is scripted via MockRuntime.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field

from refract.builtins import BUILTINS, BuiltinDef
from refract.events import EventWriter
from refract.models.agent import AgentSpec, Port
from refract.models.ledger import NodeStatus, RunStatus, StepOutcome
from refract.models.pipeline import Pipeline
from refract.models.types import (
    CollectionItem,
    CollectionManifest,
    CollectionStats,
    CollectionStatus,
)
from refract.runtime.base import EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import run_pipeline
from refract.state import Ledger

from graph_fixtures import agent_spec, write_registry

# --- shared builders (mirrors tests/test_scheduler.py) -----------------------


def _clock_seq() -> "callable":
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


async def _no_sleep(_seconds: float) -> None:
    return None


def _write_agent_pkg(run_dir: Path, ref: str) -> None:
    pkg_dir = run_dir / "snapshot" / "agents" / ref
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "prompt.md").write_text(f"You are {ref}.", encoding="utf-8")


def _agents(*specs: AgentSpec) -> dict[str, AgentSpec]:
    return {s.ref: s for s in specs}


def _ledger(run_dir: Path, node_ids: list[str], *, pipeline_name: str = "p") -> Ledger:
    return Ledger.create(
        run_dir,
        run_id="run_test",
        pipeline=pipeline_name,
        node_ids=node_ids,
        created_at="T0",
    )


# --- builtin/seed: a controllable collection producer for map tests ---------


class _SeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    source: str = "src"
    source_hash: str = "sha256:seed"
    status: str = "ok"
    payload: dict[str, str] = Field(default_factory=dict)


class _SeedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_SeedItem] = Field(default_factory=list)


def _seed_run(
    *, params: _SeedParams, input_dir: Path, output_dir: Path, port: str
) -> CollectionManifest:
    collection_dir = Path(output_dir) / port
    collection_dir.mkdir(parents=True, exist_ok=True)
    items: list[CollectionItem] = []
    ok = failed = 0
    for it in params.items:
        if it.status == "ok":
            slug_dir = collection_dir / it.slug
            slug_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in it.payload.items():
                (slug_dir / rel).write_text(content, encoding="utf-8")
            ok += 1
            error = None
        else:
            failed += 1
            error = "seed: pre-failed input item"
        items.append(
            CollectionItem(
                slug=it.slug,
                source=it.source,
                source_hash=it.source_hash,
                status=CollectionStatus(it.status),
                path=f"{it.slug}/",
                error=error,
            )
        )
    manifest = CollectionManifest(
        type="collection<source@v1>",
        items=items,
        stats=CollectionStats(total=len(items), ok=ok, failed=failed),
    )
    (collection_dir / "_collection.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture(autouse=True)
def _register_seed_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        BUILTINS,
        "seed",
        BuiltinDef(
            params_model=_SeedParams,
            produces=[Port(port="sources", type="collection<source@v1>")],
            run=_seed_run,
        ),
    )


def _seed_node_yaml(items: list[dict]) -> str:
    return yaml.safe_dump({"items": items})


def _source_processor() -> AgentSpec:
    return agent_spec(
        "source_processor",
        consumes=[{"port": "source", "type": "source@v1"}],
        produces=[{"port": "extract", "type": "extract@v1"}],
    )


def _map_pipeline(seed_items: list[dict], *, extra_params: str = "") -> Pipeline:
    seed_yaml = json.dumps(seed_items)
    doc = f"""
version: "0.1"
name: mapdemo
nodes:
  - id: scan
    type: builtin/seed
    params: {{ items: {seed_yaml} }}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: {{ model: "mock/mock-1", gate_retries: 0, infra_retries: 0{extra_params} }}
"""
    return Pipeline.model_validate(yaml.safe_load(doc))


async def _run(
    tmp_path: Path,
    pipeline: Pipeline,
    runtime: object,
    *,
    node_ids: list[str] = ["scan", "extract"],
) -> tuple[RunStatus, Ledger, Path]:
    run_dir = tmp_path / "run"
    registry = write_registry(tmp_path)
    agents = _agents(_source_processor())
    _write_agent_pkg(run_dir, "source_processor@1")

    ledger = _ledger(run_dir, node_ids, pipeline_name="mapdemo")
    events = EventWriter(run_dir, clock=_clock_seq())

    status = await run_pipeline(
        run_dir,
        pipeline=pipeline,
        agents=agents,
        registry=registry,
        runtime=runtime,
        ledger=ledger,
        events=events,
        clock=_clock_seq(),
        sleeper=_no_sleep,
    )
    return status, ledger, run_dir


def _out_manifest(run_dir: Path) -> CollectionManifest:
    raw = json.loads(
        (
            run_dir / "steps" / "extract" / "_out" / "extract" / "_collection.json"
        ).read_text("utf-8")
    )
    return CollectionManifest.model_validate(raw)


# --- 1. happy path -----------------------------------------------------------


class TestMapHappyPath:
    async def test_fans_out_one_step_per_ok_item_and_assembles_output(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.3: one step per ok collection item, reassembled under
        # steps/<node>/_out/<port>/ with a valid manifest.
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
            ]
        )
        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
                "extract:b": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "b"})})
                ],
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.completed
        assert ledger.get_node("extract").status is NodeStatus.done
        assert ledger.get_step("extract:a").outcome is StepOutcome.ok
        assert ledger.get_step("extract:b").outcome is StepOutcome.ok

        manifest = _out_manifest(run_dir)
        assert manifest.type == "collection<extract@v1>"
        assert manifest.stats.total == 2
        assert manifest.stats.ok == 2
        assert manifest.stats.failed == 0
        by_slug = {i.slug: i for i in manifest.items}
        assert by_slug["a"].status is CollectionStatus.ok
        assert by_slug["b"].status is CollectionStatus.ok

        payload_a = (
            run_dir / "steps" / "extract" / "_out" / "extract" / "a" / "extract.json"
        )
        assert payload_a.exists()
        assert json.loads(payload_a.read_text("utf-8")) == {"v": "a"}


# --- 2. failed input items are skipped but carried into the output ----------


class TestMapSkipsFailedInputItems:
    async def test_pre_failed_input_item_is_not_run_but_copied_as_failed(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.3 / CLAUDE.md gotcha: map skips failed input items (no step
        # runs for them) but copies them into the output collection as failed.
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "bad", "status": "failed"},
            ]
        )
        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.completed
        assert ledger.get_node("extract").status is NodeStatus.done
        # no step was ever executed for the pre-failed item
        assert ledger.get_step("extract:bad") is None

        manifest = _out_manifest(run_dir)
        assert manifest.stats.total == 2
        assert manifest.stats.ok == 1
        assert manifest.stats.failed == 1
        by_slug = {i.slug: i for i in manifest.items}
        assert by_slug["a"].status is CollectionStatus.ok
        assert by_slug["bad"].status is CollectionStatus.failed
        assert by_slug["bad"].error is not None
        bad_dir = run_dir / "steps" / "extract" / "_out" / "extract" / "bad"
        assert not bad_dir.exists()


# --- 3. a step that fails at runtime is recorded failed in the manifest -----


class TestMapRuntimeStepFailure:
    async def test_gate_failed_element_step_recorded_failed_in_manifest(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.3: an ok input item whose element step fails is recorded
        # status: failed in the output manifest (on_item_failure=skip by default
        # keeps the node itself green as long as min_ok is satisfied).
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
            ],
            extra_params=", min_ok: 1",
        )
        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
                # "b" never writes the required artifact -> gate fails
                "extract:b": [ScriptedResponse(files={})],
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.completed
        assert ledger.get_node("extract").status is NodeStatus.done
        assert ledger.get_step("extract:b").outcome is not StepOutcome.ok

        manifest = _out_manifest(run_dir)
        by_slug = {i.slug: i for i in manifest.items}
        assert by_slug["a"].status is CollectionStatus.ok
        assert by_slug["b"].status is CollectionStatus.failed
        assert manifest.stats.ok == 1
        assert manifest.stats.failed == 1


# --- 4. min_ok --------------------------------------------------------------


class TestMapMinOk:
    async def test_node_fails_when_ok_count_below_min_ok(self, tmp_path: Path) -> None:
        # SPEC §10.3: node fails when ok_count < min_ok, even though the run
        # otherwise completed its per-item fan-out.
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
            ],
            extra_params=", min_ok: 2",
        )
        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
                "extract:b": [ScriptedResponse(files={})],  # gate fails
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.failed
        assert ledger.get_node("extract").status is NodeStatus.failed
        manifest = _out_manifest(run_dir)
        assert manifest.stats.ok == 1
        assert manifest.stats.failed == 1


# --- 5. on_item_failure: fail --------------------------------------------


class TestMapOnItemFailureFail:
    async def test_node_fails_on_any_item_failure_when_configured(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.3: on_item_failure=fail fails the node on any item failure,
        # even if min_ok would otherwise have been satisfied.
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
            ],
            extra_params=', min_ok: 1, on_item_failure: "fail"',
        )
        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
                "extract:b": [ScriptedResponse(files={})],  # gate fails
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.failed
        assert ledger.get_node("extract").status is NodeStatus.failed


# --- 6. resume idempotency ----------------------------------------------------


class TestMapResumeIdempotency:
    async def test_done_element_steps_are_reused_and_output_rebuilt(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.5 crash-resume + §10.3: element steps already `done` in the
        # ledger are not re-executed; the output collection is always rebuilt
        # from scratch, so re-assembly on resume is idempotent.
        run_dir = tmp_path / "run"
        registry = write_registry(tmp_path)
        agents = _agents(_source_processor())
        _write_agent_pkg(run_dir, "source_processor@1")

        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
            ]
        )

        node_ids = ["scan", "extract"]
        ledger = _ledger(run_dir, node_ids, pipeline_name="mapdemo")
        events = EventWriter(run_dir, clock=_clock_seq())

        runtime = MockRuntime(
            {
                "extract:a": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "a"})})
                ],
                "extract:b": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": "b"})})
                ],
            }
        )

        status1 = await run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=agents,
            registry=registry,
            runtime=runtime,
            ledger=ledger,
            events=events,
            clock=_clock_seq(),
            sleeper=_no_sleep,
        )
        assert status1 is RunStatus.completed
        tries_a_first = ledger.get_step("extract:a").tries

        # Simulate crash-resume: reload the ledger from disk (running steps
        # flip to pending on load), rerun on a runtime that would error if
        # asked to redo "a"/"b" (only allows a fresh call count of 0).
        ledger2 = Ledger.load(run_dir)
        events2 = EventWriter(run_dir, clock=_clock_seq())

        class _NoRerunRuntime:
            async def run_step(
                self, spec: StepSpec, on_event: EventCallback
            ) -> StepResult:
                raise AssertionError(
                    f"element step {spec.step_id} should not be re-executed on resume"
                )

            async def close(self) -> None:
                return None

        status2 = await run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=agents,
            registry=registry,
            runtime=_NoRerunRuntime(),
            ledger=ledger2,
            events=events2,
            clock=_clock_seq(),
            sleeper=_no_sleep,
        )

        assert status2 is RunStatus.completed
        assert ledger2.get_step("extract:a").tries == tries_a_first
        assert ledger2.get_node("extract").status is NodeStatus.done

        manifest = _out_manifest(run_dir)
        assert manifest.stats.total == 2
        assert manifest.stats.ok == 2


# --- 7. workers/provider semaphores bound concurrency ------------------------


class _ConcurrencyTrackingRuntime:
    """Records max concurrent ``run_step`` calls (used to test bounded
    concurrency, since MockRuntime alone can't observe overlap)."""

    def __init__(self, files_by_step: dict[str, dict[str, str]]) -> None:
        self._files_by_step = files_by_step
        self._current = 0
        self.max_concurrency = 0

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        self._current += 1
        self.max_concurrency = max(self.max_concurrency, self._current)
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            output_dir = spec.workdir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in self._files_by_step[spec.step_id].items():
                (output_dir / rel).write_text(content, encoding="utf-8")
            (spec.workdir / "raw.txt").write_text("mock", encoding="utf-8")
            (spec.workdir / "agent.events.jsonl").write_text("", encoding="utf-8")
            return StepResult(completed=True)
        finally:
            self._current -= 1

    async def close(self) -> None:
        return None


class TestMapWorkersSemaphore:
    async def test_workers_limit_one_serializes_element_steps(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.3: params.workers bounds per-node element concurrency.
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
                {"slug": "c", "payload": {"src.txt": "C"}},
            ],
            extra_params=", workers: 1",
        )
        runtime = _ConcurrencyTrackingRuntime(
            {
                "extract:a": {"extract.json": json.dumps({"v": "a"})},
                "extract:b": {"extract.json": json.dumps({"v": "b"})},
                "extract:c": {"extract.json": json.dumps({"v": "c"})},
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.completed
        assert runtime.max_concurrency == 1

    async def test_workers_limit_three_allows_full_concurrency(
        self, tmp_path: Path
    ) -> None:
        pipeline = _map_pipeline(
            [
                {"slug": "a", "payload": {"src.txt": "A"}},
                {"slug": "b", "payload": {"src.txt": "B"}},
                {"slug": "c", "payload": {"src.txt": "C"}},
            ],
            extra_params=", workers: 3",
        )
        runtime = _ConcurrencyTrackingRuntime(
            {
                "extract:a": {"extract.json": json.dumps({"v": "a"})},
                "extract:b": {"extract.json": json.dumps({"v": "b"})},
                "extract:c": {"extract.json": json.dumps({"v": "c"})},
            }
        )
        status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)

        assert status is RunStatus.completed
        assert runtime.max_concurrency == 3


# --- gate_rules on a map node run per element (SPEC §8/§5.1) ----------------
#
# Added after the same field turned out never to run on a loop body: `_plan` was reading
# it off `block.params`, which has no such field, and nothing noticed for as long as the
# feature existed. The map path passes `node.gate_rules` directly and is therefore
# correct — but it was equally unproven, and an unproven live path is how the loop defect
# survived 645 passing tests.


@pytest.mark.asyncio
async def test_map_gate_rules_apply_to_every_element(tmp_path: Path) -> None:
    doc = """
version: "0.1"
name: mapdemo
nodes:
  - id: scan
    type: builtin/seed
    params: { items: [{"slug": "a", "payload": {"src.txt": "A"}}, {"slug": "b", "payload": {"src.txt": "B"}}] }
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: { model: "mock/mock-1", gate_retries: 0, infra_retries: 0, min_ok: 1 }
    gate_rules:
      - { rule: min_length, value: 5000 }
"""
    pipeline = Pipeline.model_validate(yaml.safe_load(doc))
    runtime = MockRuntime(
        {"extract:*": [ScriptedResponse(files={"extract.json": '{"x": 1}'})]}
    )
    status, ledger, run_dir = await _run(tmp_path, pipeline, runtime)
    # every element violates the rule, so every element fails and the node cannot pass
    assert status is RunStatus.failed
    for slug in ("a", "b"):
        report = json.loads(
            (run_dir / "steps" / "extract" / slug / "gate_report.json").read_text("utf-8")
        )
        assert report["ok"] is False, slug
        assert any("min_length 5000" in p for p in report["ports"][0]["problems"]), slug


@pytest.mark.asyncio
async def test_map_gate_rules_reach_each_element_prompt(tmp_path: Path) -> None:
    """The requirement is generated into the prompt from the same list (I5)."""
    doc = """
version: "0.1"
name: mapdemo
nodes:
  - id: scan
    type: builtin/seed
    params: { items: [{"slug": "a", "payload": {"src.txt": "A"}}] }
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: { model: "mock/mock-1", gate_retries: 0, infra_retries: 0, min_ok: 1 }
    gate_rules:
      - { rule: min_length, value: 4242 }
"""
    pipeline = Pipeline.model_validate(yaml.safe_load(doc))
    runtime = MockRuntime(
        {"extract:*": [ScriptedResponse(files={"extract.json": '{"x": 1}'})]}
    )
    _status, _ledger, run_dir = await _run(tmp_path, pipeline, runtime)
    prompt = run_dir / "steps" / "extract" / "a" / "prompt.md"
    assert "At least 4242 characters" in prompt.read_text("utf-8")
