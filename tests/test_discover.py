"""Tests for the ``discover`` node — network source of a collection (SPEC §20).

The agent produces ONE directory artifact; the engine turns it into
``collection<source@v1>``, which is what keeps I6 intact. MockRuntime stands in for
the searching agent — no network, no real CLI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from refract.builtins import BUILTINS
from refract.events import EventWriter
from refract.models.agent import AgentSpec
from refract.models.ledger import NodeStatus, RunStatus
from refract.models.pipeline import Pipeline
from refract.runtime.mock import MockRuntime, ScriptedResponse
from refract.scheduler import assemble_discovered_collection, run_pipeline
from refract.state import Ledger

from graph_fixtures import agent_spec, write_registry

FOUND = {
    "found/alpha.md": "# Alpha\nFirst find.\n",
    "found/beta.md": "# Beta\nSecond find.\n",
    "found/gamma.md": "# Gamma\nThird find.\n",
    "found/_index.json": json.dumps(
        [
            {"file": "alpha.md", "title": "Alpha", "url": "https://example.com/a"},
            {"file": "beta.md", "title": "Beta", "url": "https://example.com/b"},
            {"file": "gamma.md", "title": "Gamma", "url": "https://example.com/g"},
        ]
    ),
}
EXTRACT = json.dumps({"source": "s", "requirements": [], "trust_level": "low"})


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
    (pkg_dir / "agent.yaml").write_text(f"name: {ref}\n", encoding="utf-8")


def _finder() -> AgentSpec:
    return agent_spec(
        "source_finder",
        consumes=[{"port": "brief", "type": "brief@v1"}],
        produces=[{"port": "found", "type": "found_sources@v1"}],
        needs=["read", "edit", "mcp:tavily-remote"],
    )


def _processor() -> AgentSpec:
    return agent_spec(
        "source_processor",
        consumes=[{"port": "source", "type": "source@v1"}],
        produces=[{"port": "extract", "type": "extract@v1"}],
    )


def _pipeline(*, min_sources: int = 1, with_map: bool = True) -> Pipeline:
    nodes = """
  - id: brief
    type: builtin/brief
  - id: find
    type: discover
    agent: source_finder@1
    inputs: { brief: brief.brief }
    params: { model: kimi/k3, min_sources: MIN }
"""
    if with_map:
        nodes += """  - id: extract
    type: agent
    agent: source_processor@1
    map: find.sources
    params: { model: kimi/k3, workers: 2 }
"""
    text = f'version: "0.1"\nname: research\ninput_mode: brief\nnodes:{nodes}'.replace(
        "MIN", str(min_sources)
    )
    return Pipeline.model_validate(yaml.safe_load(text))


def _run(
    tmp_path: Path,
    *,
    pipeline: Pipeline,
    found: dict[str, str],
    brief: str = "Research offline-first warehouse scanning.",
) -> tuple[RunStatus, Path, Ledger]:
    registry = write_registry(tmp_path)
    project_input = tmp_path / "input"
    project_input.mkdir(exist_ok=True)
    (project_input / "brief.md").write_text(brief, encoding="utf-8")

    run_dir = tmp_path / "run"
    specs = {s.ref: s for s in (_finder(), _processor())}
    for ref in specs:
        _write_agent_pkg(run_dir, ref)
    ledger = Ledger.create(
        run_dir,
        run_id="run_test",
        pipeline="research",
        node_ids=[n.id for n in pipeline.nodes],
        created_at="T0",
    )
    runtime = MockRuntime(
        {
            "find": [ScriptedResponse(files=found)],
            "extract:*": [ScriptedResponse(files={"extract.json": EXTRACT})],
        }
    )
    status = asyncio.run(
        run_pipeline(
            run_dir,
            pipeline=pipeline,
            agents=specs,
            registry=registry,
            runtime=runtime,
            ledger=ledger,
            events=EventWriter(run_dir, clock=_clock_seq()),
            project_input_dir=project_input,
            clock=_clock_seq(),
            sleeper=_no_sleep,
        )
    )
    return status, run_dir, ledger


class TestFanOut:
    def test_agent_dir_becomes_a_collection_that_map_fans_out_over(
        self, tmp_path: Path
    ) -> None:
        # SPEC §20.1/§20.2: the agent produced ONE dir; the engine made the
        # collection, and the downstream map ran once per discovered source.
        status, run_dir, ledger = _run(tmp_path, pipeline=_pipeline(), found=FOUND)

        assert status is RunStatus.completed, {
            k: (v.status.value, v.error) for k, v in ledger.state.nodes.items()
        }
        manifest = json.loads(
            (
                run_dir / "steps" / "find" / "_out" / "sources" / "_collection.json"
            ).read_text("utf-8")
        )
        assert manifest["type"] == "collection<source@v1>"
        assert [i["slug"] for i in manifest["items"]] == [
            "alpha-md",
            "beta-md",
            "gamma-md",
        ]
        assert manifest["stats"] == {"total": 3, "ok": 3, "failed": 0}
        # one map step per discovered source, each with its payload materialized
        for slug in ("alpha-md", "beta-md", "gamma-md"):
            assert ledger.state.steps[f"extract:{slug}"].status.value == "done"
        assert ledger.get_node("extract").status is NodeStatus.done

    def test_index_json_is_metadata_not_a_source(self, tmp_path: Path) -> None:
        _, run_dir, _ = _run(tmp_path, pipeline=_pipeline(), found=FOUND)

        out = run_dir / "steps" / "find" / "_out" / "sources"
        assert (out / "_index.json").is_file()  # kept beside the manifest
        assert not (out / "-index-json").exists()  # never became an element
        manifest = json.loads((out / "_collection.json").read_text("utf-8"))
        assert all(i["source"] != "_index.json" for i in manifest["items"])

    def test_source_hash_matches_the_scanner_rules(self, tmp_path: Path) -> None:
        # SPEC §20.2: one hashing scheme for both collection producers, so a rerun
        # that finds the same document reuses the downstream map step.
        from refract.builtins.scanner import source_hash

        _, run_dir, _ = _run(tmp_path, pipeline=_pipeline(), found=FOUND)

        manifest = json.loads(
            (
                run_dir / "steps" / "find" / "_out" / "sources" / "_collection.json"
            ).read_text("utf-8")
        )
        alpha = next(i for i in manifest["items"] if i["slug"] == "alpha-md")
        expected = source_hash(
            run_dir / "steps" / "find" / "main" / "output" / "found" / "alpha.md"
        )
        assert alpha["source_hash"] == expected


class TestFailureModes:
    def test_empty_agent_output_fails_the_gate(self, tmp_path: Path) -> None:
        # A dir artifact must be non-empty (SPEC §10.2 CHANGED): an agent that
        # found nothing must not pass as ok.
        status, _, ledger = _run(
            tmp_path, pipeline=_pipeline(with_map=False), found={"found/.keep": ""}
        )

        assert status is RunStatus.failed
        assert ledger.get_node("find").status is NodeStatus.failed
        assert ledger.state.steps["find"].outcome.value == "failed_validation"

    def test_fewer_sources_than_min_sources_fails_the_node(
        self, tmp_path: Path
    ) -> None:
        status, _, ledger = _run(
            tmp_path,
            pipeline=_pipeline(min_sources=3, with_map=False),
            found={"found/only.md": "# Only\n"},
        )

        assert status is RunStatus.failed
        node = ledger.get_node("find")
        assert node.status is NodeStatus.failed
        assert "min_sources=3" in (node.error or "")

    def test_missing_brief_fails_the_brief_builtin(self, tmp_path: Path) -> None:
        # SPEC §20.4: no brief means every downstream node is unreachable.
        registry = write_registry(tmp_path)
        empty_input = tmp_path / "empty"
        empty_input.mkdir()
        pipeline = _pipeline(with_map=False)
        run_dir = tmp_path / "run"
        specs = {s.ref: s for s in (_finder(),)}
        _write_agent_pkg(run_dir, "source_finder@1")
        ledger = Ledger.create(
            run_dir,
            run_id="run_test",
            pipeline="research",
            node_ids=[n.id for n in pipeline.nodes],
            created_at="T0",
        )

        status = asyncio.run(
            run_pipeline(
                run_dir,
                pipeline=pipeline,
                agents=specs,
                registry=registry,
                runtime=MockRuntime({}),
                ledger=ledger,
                events=EventWriter(run_dir, clock=_clock_seq()),
                project_input_dir=empty_input,
                clock=_clock_seq(),
                sleeper=_no_sleep,
            )
        )

        assert status is RunStatus.failed
        assert ledger.get_node("brief").status is NodeStatus.failed
        assert ledger.get_node("find").status is NodeStatus.skipped


class TestAssemblyDirectly:
    def test_assembly_is_idempotent(self, tmp_path: Path) -> None:
        # SPEC §20.2: rebuilt from scratch, so resume re-assembly is safe.
        found = tmp_path / "found"
        found.mkdir()
        (found / "a.md").write_text("A", encoding="utf-8")
        out = tmp_path / "out"

        first = assemble_discovered_collection(found, out)
        (out / "stale").mkdir()  # debris a partial prior assembly could leave
        second = assemble_discovered_collection(found, out)

        assert first.model_dump() == second.model_dump()
        assert not (out / "stale").exists()
        assert sorted(p.name for p in out.iterdir()) == ["_collection.json", "a-md"]

    def test_subfolder_becomes_one_element(self, tmp_path: Path) -> None:
        found = tmp_path / "found"
        (found / "paper").mkdir(parents=True)
        (found / "paper" / "page1.md").write_text("1", encoding="utf-8")
        (found / "paper" / "page2.md").write_text("2", encoding="utf-8")

        manifest = assemble_discovered_collection(found, tmp_path / "out")

        assert [i["slug"] for i in manifest.model_dump()["items"]] == ["paper"]
        assert (tmp_path / "out" / "paper" / "page2.md").is_file()

    def test_brief_builtin_is_registered_and_deterministic(
        self, tmp_path: Path
    ) -> None:
        bdef = BUILTINS["brief"]
        assert bdef.produces[0].type == "brief@v1"
        source = tmp_path / "in"
        source.mkdir()
        (source / "brief.md").write_text("Topic: offline sync\n", encoding="utf-8")
        out = tmp_path / "out"

        assert bdef.run is not None
        bdef.run(
            params=bdef.params_model(),
            input_dir=source,
            output_dir=out,
            port="brief",
        )

        assert (out / "brief.md").read_text("utf-8") == "Topic: offline sync\n"

    def test_brief_builtin_rejects_an_empty_brief(self, tmp_path: Path) -> None:
        bdef = BUILTINS["brief"]
        source = tmp_path / "in"
        source.mkdir()
        (source / "brief.md").write_text("   \n", encoding="utf-8")

        assert bdef.run is not None
        with pytest.raises(ValueError, match="empty"):
            bdef.run(
                params=bdef.params_model(),
                input_dir=source,
                output_dir=tmp_path / "out",
                port="brief",
            )


class TestSnapshotAndReuse:
    def test_discover_agent_is_snapshotted(self) -> None:
        # A run executes from its snapshot: an agent the snapshot forgot cannot run
        # at all (SPEC §9). Discover nodes bind an agent like any other node.
        from refract.snapshot import used_agent_refs

        pipeline = _pipeline()

        assert "source_finder@1" in used_agent_refs(pipeline)

    def test_discover_gets_a_resolved_model_from_the_snapshot(self) -> None:
        # SPEC §20.2: the discover step is an agent step, so resolved.yaml must carry
        # its effective model — without it the run dies at plan time.
        import yaml as _yaml

        from refract.snapshot import build_resolved

        raw = Pipeline.model_validate(
            _yaml.safe_load(
                """
version: "0.1"
name: research
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: find
    type: discover
    agent: source_finder@1
    inputs: { brief: brief.brief }
"""
            )
        )

        resolved = build_resolved(
            raw,
            agents={"source_finder@1": _finder()},
            overrides={},
            default_model="kimi/k3",
        )

        find = next(n for n in resolved["nodes"] if n["id"] == "find")  # type: ignore[union-attr]
        assert find["params"]["model"] == "kimi/k3"

    def test_discover_waits_for_its_brief(self) -> None:
        # The dependency the first version missed: without it `find` started in
        # parallel with `brief` and materialized an input that did not exist yet.
        from refract.scheduler import node_dependencies

        deps = node_dependencies(_pipeline())

        assert deps["find"] == {"brief"}
        assert deps["extract"] == {"find"}
