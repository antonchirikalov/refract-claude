"""Rerun / reuse tests (SPEC §10.5, §18 ``test_reuse``).

MockRuntime only -- no network, no real CLI. The engine-level cases drive
``refract.scheduler.run_pipeline`` directly with ``reuse_run_dir`` (mirrors the
harness in tests/test_map.py / tests/test_map_over.py); one case additionally
exercises the CLI ``rerun_impl`` end to end on a copy of examples/demo-project.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from refract.events import EventWriter
from refract.models.agent import AgentSpec
from refract.models.ledger import NodeStatus, RunStatus, StepStatus
from refract.models.pipeline import Pipeline
from refract.reuse import builtin_signature, descendants, recompute_set
from refract.runtime.base import EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import run_pipeline
from refract.state import Ledger

from graph_fixtures import agent_spec, write_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _ledger(
    run_dir: Path,
    node_ids: list[str],
    *,
    pipeline_name: str = "p",
    force_nodes: list[str] | None = None,
    reuse_from: str | None = None,
) -> Ledger:
    return Ledger.create(
        run_dir,
        run_id="run_test",
        pipeline=pipeline_name,
        node_ids=node_ids,
        created_at="T0",
        force_nodes=force_nodes,
        reuse_from=reuse_from,
    )


def _source_processor() -> AgentSpec:
    return agent_spec(
        "source_processor",
        consumes=[{"port": "source", "type": "source@v1"}],
        produces=[{"port": "extract", "type": "extract@v1"}],
    )


def _requirements_writer() -> AgentSpec:
    return agent_spec(
        "requirements_writer",
        consumes=[{"port": "extracts", "type": "collection<extract@v1>"}],
        produces=[{"port": "doc", "type": "requirements@v1"}],
    )


def _pipeline() -> Pipeline:
    doc = """
version: "0.1"
name: reusedemo
nodes:
  - id: scan
    type: builtin/scanner
  - id: proc
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: { model: "mock/mock-1", gate_retries: 0, infra_retries: 0 }
  - id: write
    type: agent
    agent: requirements_writer@1
    inputs: { extracts: proc.extract }
    params: { model: "mock/mock-1", gate_retries: 0, infra_retries: 0 }
"""
    return Pipeline.model_validate(yaml.safe_load(doc))


def _write_inputs(input_dir: Path, names: list[str]) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        (input_dir / name).write_text(f"source {i}: {name}\n", encoding="utf-8")


class _TrackingRuntime:
    """Records every step_id it is asked to run_step (never touches network)."""

    def __init__(self, files_by_pattern: dict[str, dict[str, str]]) -> None:
        self._files_by_pattern = files_by_pattern
        self.executed: list[str] = []

    def _files_for(self, step_id: str) -> dict[str, str]:
        import fnmatch

        for pattern, files in self._files_by_pattern.items():
            if fnmatch.fnmatchcase(step_id, pattern):
                return files
        raise AssertionError(f"no scripted files for step {step_id!r}")

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        self.executed.append(spec.step_id)
        output_dir = spec.workdir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in self._files_for(spec.step_id).items():
            (output_dir / rel).write_text(content, encoding="utf-8")
        (spec.workdir / "raw.txt").write_text("mock", encoding="utf-8")
        (spec.workdir / "agent.events.jsonl").write_text("", encoding="utf-8")
        return StepResult(completed=True)

    async def close(self) -> None:
        return None


async def _run(
    tmp_path: Path,
    run_name: str,
    pipeline: Pipeline,
    runtime: object,
    *,
    input_names: list[str],
    force_nodes: list[str] | None = None,
    reuse_run_dir: Path | None = None,
) -> tuple[RunStatus, Ledger, Path]:
    run_dir = tmp_path / run_name
    registry = write_registry(tmp_path)
    agents = _agents(_source_processor(), _requirements_writer())
    _write_agent_pkg(run_dir, "source_processor@1")
    _write_agent_pkg(run_dir, "requirements_writer@1")

    input_dir = tmp_path / f"{run_name}-input"
    _write_inputs(input_dir, input_names)

    node_ids = [n.id for n in pipeline.nodes]
    ledger = _ledger(
        run_dir,
        node_ids,
        pipeline_name="reusedemo",
        force_nodes=force_nodes,
        reuse_from=str(reuse_run_dir) if reuse_run_dir else None,
    )
    events = EventWriter(run_dir, clock=_clock_seq())

    status = await run_pipeline(
        run_dir,
        pipeline=pipeline,
        agents=agents,
        registry=registry,
        runtime=runtime,
        ledger=ledger,
        events=events,
        project_input_dir=input_dir,
        reuse_run_dir=reuse_run_dir,
        clock=_clock_seq(),
        sleeper=_no_sleep,
    )
    return status, ledger, run_dir


def _proc_manifest(run_dir: Path) -> dict:
    return json.loads(
        (
            run_dir / "steps" / "proc" / "_out" / "extract" / "_collection.json"
        ).read_text("utf-8")
    )


# --- 1. rerun-from-node: only the forced node + descendants recompute --------


class TestRerunFromNode:
    async def test_upstream_map_node_reused_wholesale_downstream_recomputes(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.5: force_nodes=["write"] -> R = {write} (no descendants). scan
        # (builtin) always re-executes but is unchanged; proc is outside R and
        # its inputs (scan's output) are unchanged, so it is REUSED wholesale
        # (node + its element steps); write is in R and recomputes.
        pipeline = _pipeline()
        runtime_a = MockRuntime(
            {
                "proc:*": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": 1})})
                ],
                "write": [ScriptedResponse(files={"doc.md": "# Requirements: x\n"})],
            }
        )
        status_a, ledger_a, run_a = await _run(
            tmp_path, "run_a", pipeline, runtime_a, input_names=["a.txt", "b.txt"]
        )
        assert status_a is RunStatus.completed
        assert ledger_a.get_step("proc:a-txt").outcome is not None

        tracking = _TrackingRuntime({"write": {"doc.md": "# Requirements: y\n"}})
        status_b, ledger_b, run_b = await _run(
            tmp_path,
            "run_b",
            pipeline,
            tracking,
            input_names=["a.txt", "b.txt"],
            force_nodes=["write"],
            reuse_run_dir=run_a,
        )
        assert status_b is RunStatus.completed
        # proc:* was never asked to run again -- reused wholesale.
        assert not any(sid.startswith("proc:") for sid in tracking.executed)
        assert tracking.executed == ["write"]

        assert ledger_b.get_node("proc").status is NodeStatus.reused
        assert ledger_b.get_step("proc:a-txt").status is StepStatus.reused
        assert ledger_b.get_step("proc:b-txt").status is StepStatus.reused
        assert ledger_b.get_node("write").status is NodeStatus.done

        # the reused proc output collection was carried over intact
        manifest = _proc_manifest(run_b)
        assert manifest["stats"] == {"total": 2, "ok": 2, "failed": 0}


# --- 2. map element diff by (slug, source_hash) ------------------------------


class TestMapElementDiffBySourceHash:
    async def test_new_input_item_recomputed_others_reused(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.5: rerun with force_nodes=["scan"] puts proc in R (descendant
        # of scan). Adding a 3rd input file: proc's element diff by
        # (slug, source_hash) reuses the two unchanged element steps and only
        # executes the new one.
        pipeline = _pipeline()
        runtime_a = MockRuntime(
            {
                "proc:*": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": 1})})
                ],
                "write": [ScriptedResponse(files={"doc.md": "# Requirements: x\n"})],
            }
        )
        status_a, ledger_a, run_a = await _run(
            tmp_path, "run_a", pipeline, runtime_a, input_names=["a.txt", "b.txt"]
        )
        assert status_a is RunStatus.completed

        tracking = _TrackingRuntime(
            {
                "proc:*": {"extract.json": json.dumps({"v": 1})},
                "write": {"doc.md": "# Requirements: z\n"},
            }
        )
        status_b, ledger_b, run_b = await _run(
            tmp_path,
            "run_b",
            pipeline,
            tracking,
            input_names=["a.txt", "b.txt", "c.txt"],
            force_nodes=["scan"],
            reuse_run_dir=run_a,
        )
        assert status_b is RunStatus.completed
        # only the new element ("c.txt" -> slug "c-txt") was actually executed
        proc_executed = [s for s in tracking.executed if s.startswith("proc:")]
        assert proc_executed == ["proc:c-txt"]

        assert ledger_b.get_step("proc:a-txt").status is StepStatus.reused
        assert ledger_b.get_step("proc:b-txt").status is StepStatus.reused
        assert ledger_b.get_step("proc:c-txt").status is StepStatus.done

        manifest = _proc_manifest(run_b)
        assert manifest["stats"] == {"total": 3, "ok": 3, "failed": 0}
        slugs = {i["slug"] for i in manifest["items"]}
        assert slugs == {"a-txt", "b-txt", "c-txt"}


# --- 3. transitive invalidation: forcing an upstream node recomputes descendants


class TestTransitiveInvalidation:
    async def test_forcing_middle_node_also_recomputes_its_descendant(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.5: force_nodes=["proc"] -> R = {proc} ∪ descendants(proc) =
        # {proc, write}. ``write`` must recompute even though only ``proc`` was
        # named explicitly.
        pipeline = _pipeline()
        runtime_a = MockRuntime(
            {
                "proc:*": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": 1})})
                ],
                "write": [ScriptedResponse(files={"doc.md": "# Requirements: x\n"})],
            }
        )
        status_a, _, run_a = await _run(
            tmp_path, "run_a", pipeline, runtime_a, input_names=["a.txt", "b.txt"]
        )
        assert status_a is RunStatus.completed

        tracking = _TrackingRuntime(
            {
                "proc:*": {"extract.json": json.dumps({"v": 2})},
                "write": {"doc.md": "# Requirements: w\n"},
            }
        )
        status_b, ledger_b, run_b = await _run(
            tmp_path,
            "run_b",
            pipeline,
            tracking,
            input_names=["a.txt", "b.txt"],
            force_nodes=["proc"],
            reuse_run_dir=run_a,
        )
        assert status_b is RunStatus.completed
        assert "write" in tracking.executed
        assert ledger_b.get_node("write").status is NodeStatus.done
        assert ledger_b.get_node("proc").status is NodeStatus.done


# --- 3b. builtin change-detection drives out-of-R invalidation (both ways) ---


class TestBuiltinChangePropagation:
    async def test_changed_builtin_invalidates_out_of_R_descendants(
        self, tmp_path: Path
    ) -> None:
        # SPEC §10.5: with force_nodes=[] (empty R), a builtin whose output CHANGES
        # (new input file) must invalidate its otherwise-reusable descendants via
        # the changed_nodes propagation: proc re-runs (element diff → only the new
        # element executes) and write re-runs, though neither is in R.
        pipeline = _pipeline()
        runtime_a = MockRuntime(
            {
                "proc:*": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": 1})})
                ],
                "write": [ScriptedResponse(files={"doc.md": "# Requirements: x\n"})],
            }
        )
        status_a, _, run_a = await _run(
            tmp_path, "run_a", pipeline, runtime_a, input_names=["a.txt", "b.txt"]
        )
        assert status_a is RunStatus.completed

        tracking = _TrackingRuntime(
            {
                "proc:*": {"extract.json": json.dumps({"v": 1})},
                "write": {"doc.md": "# Requirements: z\n"},
            }
        )
        status_b, ledger_b, _ = await _run(
            tmp_path,
            "run_b",
            pipeline,
            tracking,
            input_names=["a.txt", "b.txt", "c.txt"],  # scan output changes
            force_nodes=[],  # empty R: only the builtin change drives recompute
            reuse_run_dir=run_a,
        )
        assert status_b is RunStatus.completed
        assert tracking.executed == ["proc:c-txt", "write"]  # a/b reused, c new
        assert ledger_b.get_step("proc:a-txt").status is StepStatus.reused
        assert ledger_b.get_node("write").status is NodeStatus.done

    async def test_unchanged_builtin_keeps_out_of_R_descendants_reused(
        self, tmp_path: Path
    ) -> None:
        # The other direction: force_nodes=[] and identical inputs → scan re-runs
        # but is UNCHANGED, so proc and write stay reused (nothing re-executes).
        pipeline = _pipeline()
        runtime_a = MockRuntime(
            {
                "proc:*": [
                    ScriptedResponse(files={"extract.json": json.dumps({"v": 1})})
                ],
                "write": [ScriptedResponse(files={"doc.md": "# Requirements: x\n"})],
            }
        )
        _, _, run_a = await _run(
            tmp_path, "run_a", pipeline, runtime_a, input_names=["a.txt", "b.txt"]
        )
        tracking = _TrackingRuntime({"proc:*": {}, "write": {}})
        status_b, ledger_b, _ = await _run(
            tmp_path,
            "run_b",
            pipeline,
            tracking,
            input_names=["a.txt", "b.txt"],
            force_nodes=[],
            reuse_run_dir=run_a,
        )
        assert status_b is RunStatus.completed
        assert tracking.executed == []  # nothing re-executed
        assert ledger_b.get_node("proc").status is NodeStatus.reused
        assert ledger_b.get_node("write").status is NodeStatus.reused


# --- 4. CLI end-to-end: rerun_impl on a copy of examples/demo-project -------


class TestRerunImplEndToEnd:
    def test_rerun_from_scan_after_new_input_file_recomputes_map_element(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14/§10.5: `refract rerun --from scan` on the demo project
        # (scan -> write(map:demo_writer)) after adding a new input file.
        from refract.cli import AppConfig, run_impl, rerun_impl
        from refract.models.config import ProvidersFile

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        library_path = REPO_ROOT / "library"
        demo_project = REPO_ROOT / "examples" / "demo-project"
        project = tmp_path / "demo-project"
        shutil.copytree(demo_project, project, ignore=shutil.ignore_patterns("runs"))

        providers = ProvidersFile.model_validate(
            {
                "providers": {
                    "claude": {"api_key_env": "ANTHROPIC_API_KEY", "max_concurrent": 4}
                }
            }
        )
        app = AppConfig(library_path=library_path, providers=providers)

        req = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"

        def _factory_a(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
            return MockRuntime(
                {"write:*": [ScriptedResponse(files={"requirements.md": req})]}
            )

        status_a, run_a = run_impl(
            project,
            app=app,
            runtime_factory=_factory_a,
            run_id="run_a",
            clock=_clock_seq(),
        )
        assert status_a is RunStatus.completed

        # add a new input file so the scanner's element set changes
        (project / "input" / "gamma.txt").write_text("gamma source\n", encoding="utf-8")

        tracking = _TrackingRuntime({"write:*": {"requirements.md": req}})

        def _factory_b(app: AppConfig, pipeline: Pipeline) -> object:
            return tracking

        status_b, run_b = rerun_impl(
            project,
            from_node="scan",
            reuse="last",
            app=app,
            runtime_factory=_factory_b,
            run_id="run_b",
            clock=_clock_seq(),
        )
        assert status_b is RunStatus.completed
        # only the new element (gamma.txt -> slug gamma-txt) was executed
        assert tracking.executed == ["write:gamma-txt"]

        ledger_b = Ledger.load(run_b)
        assert ledger_b.get_step("write:alpha-txt").status is StepStatus.reused
        assert ledger_b.get_step("write:beta-txt").status is StepStatus.reused
        assert ledger_b.get_step("write:gamma-txt").status is StepStatus.done


# --- 5. unit tests for refract.reuse.descendants / recompute_set ------------


class TestDescendantsAndRecomputeSet:
    def test_descendants_of_a_seed_are_transitive_downstream_nodes(self) -> None:
        # a -> b -> c, a -> d (independent branch)
        deps = {
            "a": set(),
            "b": {"a"},
            "c": {"b"},
            "d": set(),
        }
        assert descendants(deps, {"a"}) == {"b", "c"}
        assert descendants(deps, {"d"}) == set()
        assert descendants(deps, {"c"}) == set()

    def test_recompute_set_is_force_nodes_union_descendants(self) -> None:
        deps = {
            "scan": set(),
            "proc": {"scan"},
            "write": {"proc"},
            "other": set(),
        }
        assert recompute_set(deps, ["proc"]) == {"proc", "write"}
        assert recompute_set(deps, ["write"]) == {"write"}
        assert recompute_set(deps, ["scan"]) == {"scan", "proc", "write"}


class TestBuiltinSignature:
    """SPEC §10.5: a builtin's output signature decides whether downstream survives.

    Only collection outputs were understood, so ``builtin/brief`` — a plain
    ``<port>.md`` with no manifest — always signed as empty, always counted as
    changed, and silently disabled reuse for every brief-driven pipeline: a
    ``rerun --from report`` re-searched the web and re-extracted every source.
    """

    def _brief_out(self, tmp_path: Path, text: str) -> Path:
        out = tmp_path / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "brief.md").write_text(text, encoding="utf-8")
        return out

    def test_a_file_output_has_a_signature(self, tmp_path: Path) -> None:
        sig = builtin_signature(self._brief_out(tmp_path, "тема"), "brief")
        assert sig != ""

    def test_same_content_signs_the_same(self, tmp_path: Path) -> None:
        a = builtin_signature(self._brief_out(tmp_path / "a", "тема"), "brief")
        b = builtin_signature(self._brief_out(tmp_path / "b", "тема"), "brief")
        assert a == b

    def test_edited_content_signs_differently(self, tmp_path: Path) -> None:
        a = builtin_signature(self._brief_out(tmp_path / "a", "тема"), "brief")
        b = builtin_signature(self._brief_out(tmp_path / "b", "інша тема"), "brief")
        assert a != b

    def test_a_collection_still_signs_by_manifest(self, tmp_path: Path) -> None:
        out = tmp_path / "output" / "sources"
        out.mkdir(parents=True)
        (out / "_collection.json").write_text(
            json.dumps({"items": [{"slug": "a", "source_hash": "h1", "status": "ok"}]}),
            encoding="utf-8",
        )
        assert builtin_signature(tmp_path / "output", "sources") == "a:h1:ok"

    def test_nothing_produced_signs_empty(self, tmp_path: Path) -> None:
        (tmp_path / "output").mkdir()
        # empty means "cannot verify" and the caller treats it as changed
        assert builtin_signature(tmp_path / "output", "brief") == ""


# --- 7. an edited agent package cannot be reused -----------------------------


class TestChangedAgentInvalidatesReuse:
    """`agents.lock.json` was written from the start and read by nobody (SPEC §10.5)."""

    def test_changed_refs_are_the_ones_whose_hash_moved(self) -> None:
        from refract.reuse import changed_agent_refs

        prior = {"a@1": "sha256:1", "b@1": "sha256:2"}
        current = {"a@1": "sha256:1", "b@1": "sha256:CHANGED", "c@1": "sha256:3"}
        # b changed; c is new to the prior run and so has no reusable output
        assert changed_agent_refs(prior, current) == {"b@1", "c@1"}

    def test_a_missing_lock_reads_as_everything_changed(self, tmp_path: Path) -> None:
        """An unreadable record must not be optimism: nothing is provably reusable."""
        from refract.reuse import changed_agent_refs, read_agents_lock

        assert read_agents_lock(tmp_path) == {}
        assert changed_agent_refs({}, {"a@1": "sha256:1"}) == {"a@1"}

    def test_node_refs_cover_every_container_element(self) -> None:
        from refract.models.pipeline import Pipeline
        from refract.snapshot import node_agent_refs

        pipeline = Pipeline.model_validate(
            yaml.safe_load(
                """
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: loop
    type: loop
    body:
      - { agent: w@1, inputs: { brief: brief.brief } }
      - { agent: fix@1, inputs: { draft: "@prev" } }
    critic: { agent: judge@1, inputs: { draft: "@body" } }
    outputs: { doc: "@body" }
"""
            )
        )
        refs = node_agent_refs(pipeline)
        # every element of the chain AND the critic: editing any of them invalidates
        assert refs["loop"] == {"w@1", "fix@1", "judge@1"}
        assert refs["brief"] == set()

    def test_editing_a_prompt_makes_the_node_recompute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reuse with nothing forced: only the edited agent's node runs again."""
        from refract.cli import AppConfig, run_impl
        from refract.models.config import ProvidersFile

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        library = tmp_path / "library"
        shutil.copytree(REPO_ROOT / "library", library)
        project = tmp_path / "demo-project"
        shutil.copytree(
            REPO_ROOT / "examples" / "demo-project",
            project,
            ignore=shutil.ignore_patterns("runs"),
        )
        providers = ProvidersFile.model_validate(
            {
                "providers": {
                    "claude": {"api_key_env": "ANTHROPIC_API_KEY", "max_concurrent": 4}
                }
            }
        )
        app = AppConfig(library_path=library, providers=providers)
        req = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"

        def _first(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
            return MockRuntime(
                {"write:*": [ScriptedResponse(files={"requirements.md": req})]}
            )

        status_a, _run_a = run_impl(
            project, app=app, runtime_factory=_first, run_id="run_a", clock=_clock_seq()
        )
        assert status_a is RunStatus.completed

        # the one thing a person actually does between runs
        prompt = library / "agents" / "demo_writer" / "prompt.md"
        prompt.write_text(
            prompt.read_text("utf-8") + "\nAnd one more instruction.\n",
            encoding="utf-8",
        )

        tracking = _TrackingRuntime({"write:*": {"requirements.md": req}})

        status_b, run_b = run_impl(
            project,
            app=app,
            runtime_factory=lambda app, pipeline: tracking,
            run_id="run_b",
            reuse_run_id="run_a",
            clock=_clock_seq(),
        )
        assert status_b is RunStatus.completed
        # nothing was forced on the command line; the changed package did it
        assert tracking.executed != [], "the edited prompt was silently reused"
        ledger_b = Ledger.load(run_b)
        assert ledger_b.get_step("write:alpha-txt").status is StepStatus.done

    def test_an_untouched_library_still_reuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: without an edit the node is reused, or the check is worthless."""
        from refract.cli import AppConfig, run_impl
        from refract.models.config import ProvidersFile

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        library = tmp_path / "library"
        shutil.copytree(REPO_ROOT / "library", library)
        project = tmp_path / "demo-project"
        shutil.copytree(
            REPO_ROOT / "examples" / "demo-project",
            project,
            ignore=shutil.ignore_patterns("runs"),
        )
        providers = ProvidersFile.model_validate(
            {
                "providers": {
                    "claude": {"api_key_env": "ANTHROPIC_API_KEY", "max_concurrent": 4}
                }
            }
        )
        app = AppConfig(library_path=library, providers=providers)
        req = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"

        def _first(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
            return MockRuntime(
                {"write:*": [ScriptedResponse(files={"requirements.md": req})]}
            )

        status_a, _ = run_impl(
            project, app=app, runtime_factory=_first, run_id="run_a", clock=_clock_seq()
        )
        assert status_a is RunStatus.completed

        tracking = _TrackingRuntime({"write:*": {"requirements.md": req}})
        status_b, run_b = run_impl(
            project,
            app=app,
            runtime_factory=lambda app, pipeline: tracking,
            run_id="run_b",
            reuse_run_id="run_a",
            clock=_clock_seq(),
        )
        assert status_b is RunStatus.completed
        assert tracking.executed == []
        assert (
            Ledger.load(run_b).get_step("write:alpha-txt").status is StepStatus.reused
        )
