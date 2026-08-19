"""Delivery of a run's declared outputs (SPEC §22).

Until this existed, the result of a run lived at
``runs/<id>/steps/restyle/main/output/article.md`` with its pictures three directories
away, and getting a readable article out meant copying by hand — which also meant the
next run silently overwrote wherever the copy had landed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from refract.deliver import deliver
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry

from graph_fixtures import agent_spec, write_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pipeline(outputs: dict[str, str]) -> Pipeline:
    doc = {
        "version": "0.1",
        "name": "p",
        "outputs": outputs,
        "nodes": [
            {"id": "scan", "type": "builtin/scanner"},
            {
                "id": "write",
                "type": "agent",
                "agent": "requirements_writer@1",
                "inputs": {"extracts": "scan.sources"},
            },
        ],
    }
    return Pipeline.model_validate(doc)


def _agents() -> dict:
    return {
        "requirements_writer@1": agent_spec(
            "requirements_writer",
            consumes=[{"port": "extracts", "type": "collection<source@v1>"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        )
    }


def _run_with_output(tmp_path: Path, *, body: str = "# Doc\n\nreal text\n") -> Path:
    run_dir = tmp_path / "run"
    out = run_dir / "steps" / "write" / "main" / "output"
    out.mkdir(parents=True)
    (out / "doc.md").write_text(body, encoding="utf-8")
    return run_dir


def test_a_file_output_arrives_under_the_declared_name(tmp_path: Path) -> None:
    """The NAME comes from the pipeline; the extension comes from the artifact."""
    run_dir = _run_with_output(tmp_path)
    registry = write_registry(tmp_path)
    report = deliver(
        run_dir,
        pipeline=_pipeline({"requirements": "write.doc"}),
        registry=registry,
        agents=_agents(),
    )
    assert report.ok
    assert report.delivered == {"requirements": "output/requirements.md"}
    assert (run_dir / "output" / "requirements.md").read_text("utf-8").startswith("# Doc")


def test_no_outputs_declared_delivers_nothing(tmp_path: Path) -> None:
    """A pipeline whose result is read in place is a fine answer, not a defect."""
    run_dir = _run_with_output(tmp_path)
    report = deliver(
        run_dir,
        pipeline=_pipeline({}),
        registry=write_registry(tmp_path),
        agents=_agents(),
    )
    assert report.ok and report.delivered == {}
    assert not (run_dir / "output").exists()


def test_a_missing_artifact_is_reported_not_skipped(tmp_path: Path) -> None:
    """Three of four things looks exactly like a complete folder."""
    run_dir = tmp_path / "run"
    (run_dir / "steps").mkdir(parents=True)
    report = deliver(
        run_dir,
        pipeline=_pipeline({"requirements": "write.doc"}),
        registry=write_registry(tmp_path),
        agents=_agents(),
    )
    assert not report.ok
    assert "was not produced" in report.missing["requirements"]
    assert any(line.startswith("MISSING") for line in report.render())


def test_delivery_is_rebuilt_not_merged(tmp_path: Path) -> None:
    """A stale artifact from an earlier attempt must not sit beside a fresh one."""
    run_dir = _run_with_output(tmp_path)
    registry = write_registry(tmp_path)
    stale = run_dir / "output" / "gone.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("from a previous delivery", encoding="utf-8")
    deliver(
        run_dir,
        pipeline=_pipeline({"requirements": "write.doc"}),
        registry=registry,
        agents=_agents(),
    )
    assert not stale.exists()
    assert (run_dir / "output" / "requirements.md").exists()


def test_an_unknown_port_is_reported(tmp_path: Path) -> None:
    run_dir = _run_with_output(tmp_path)
    report = deliver(
        run_dir,
        pipeline=_pipeline({"x": "write.nosuch"}),
        registry=write_registry(tmp_path),
        agents=_agents(),
    )
    assert not report.ok and "x" in report.missing


# --- the shipped explainer template: the layout its article actually needs ----


def test_explainer_declares_the_layout_its_links_need() -> None:
    """The article writes `figures/<slug>.png`, so the directory must arrive as
    `figures` — the port is called `illustration`, and only the pipeline knows which
    of the two the artifact promised."""
    doc = yaml.safe_load(
        (REPO_ROOT / "library" / "templates" / "explainer_article.yaml").read_text("utf-8")
    )
    outputs = Pipeline.model_validate(doc).outputs
    assert outputs == {"article": "restyle.article", "figures": "figures.illustration"}


def test_a_dir_output_arrives_as_a_directory(tmp_path: Path) -> None:
    """`illustration@v1` is kind=dir: it is delivered as a folder, not a file."""
    library = REPO_ROOT / "library"
    registry = ArtifactRegistry.load(library)
    run_dir = tmp_path / "run"
    src = run_dir / "steps" / "figures" / "main" / "output" / "illustration"
    src.mkdir(parents=True)
    (src / "x-to-qkv.png").write_bytes(b"\x89PNG" + b"0" * 32)
    (src / "manifest.json").write_text(json.dumps({"figures": []}), encoding="utf-8")

    agents = {
        "illustrator@1": agent_spec(
            "illustrator",
            consumes=[{"port": "article", "type": "article@v1"}],
            produces=[{"port": "illustration", "type": "illustration@v1"}],
        )
    }
    pipeline = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "p",
            "outputs": {"figures": "figures.illustration"},
            "nodes": [
                {
                    "id": "figures",
                    "type": "agent",
                    "agent": "illustrator@1",
                    "inputs": {},
                }
            ],
        }
    )
    report = deliver(run_dir, pipeline=pipeline, registry=registry, agents=agents)
    assert report.ok, report.missing
    delivered = run_dir / "output" / "figures"
    assert delivered.is_dir()
    assert (delivered / "x-to-qkv.png").exists()


# --- a completed run delivers itself (SPEC §22) ------------------------------


def test_a_completed_run_delivers_without_being_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result of a run has to be in one place the moment the run ends.

    Assembling it by hand is what this replaces, and a hand-assembled folder is also
    what the NEXT run silently overwrites.
    """
    from refract.cli import AppConfig, run_impl
    from refract.models.config import ProvidersFile
    from refract.models.ledger import RunStatus
    from refract.runtime.mock import MockRuntime, ScriptedResponse

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    project = tmp_path / "demo-project"
    shutil.copytree(
        REPO_ROOT / "examples" / "demo-project",
        project,
        ignore=shutil.ignore_patterns("runs"),
    )
    # declare a deliverable on the demo pipeline
    pipe = next((project / "pipelines").glob("*.yaml"))
    doc = yaml.safe_load(pipe.read_text("utf-8"))
    doc["outputs"] = {"requirements": "write.requirements"}
    pipe.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")

    providers = ProvidersFile.model_validate(
        {"providers": {"claude": {"api_key_env": "ANTHROPIC_API_KEY"}}}
    )
    app = AppConfig(library_path=REPO_ROOT / "library", providers=providers)
    req = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"

    status, run_dir = run_impl(
        project,
        app=app,
        runtime_factory=lambda a, p: MockRuntime(
            {"write:*": [ScriptedResponse(files={"requirements.md": req})]}
        ),
        run_id="r1",
    )
    assert status is RunStatus.completed
    # a map node's port is a collection: it arrives as a directory named by the pipeline
    delivered = run_dir / "output" / "requirements"
    assert delivered.is_dir(), sorted((run_dir / "output").iterdir())
    assert (delivered / "_collection.json").exists()


# --- existence is not enough (SPEC §22) --------------------------------------


def test_an_empty_directory_is_missing_not_delivered(tmp_path: Path) -> None:
    """Caught on a live run: an empty `figures` directory was reported as delivered.

    The gate already knows existence is not enough — a `dir` artifact is gated on having
    real content, because an agent that produced nothing still leaves a directory behind.
    Delivery checked existence alone, which is this module's own stated failure mode one
    level down: a folder holding three of four things looks exactly like a complete one.
    """
    library = REPO_ROOT / "library"
    registry = ArtifactRegistry.load(library)
    run_dir = tmp_path / "run"
    (run_dir / "steps" / "figures" / "main" / "output" / "illustration").mkdir(parents=True)

    agents = {
        "illustrator@1": agent_spec(
            "illustrator",
            consumes=[{"port": "article", "type": "article@v1"}],
            produces=[{"port": "illustration", "type": "illustration@v1"}],
        )
    }
    pipeline = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "p",
            "outputs": {"figures": "figures.illustration"},
            "nodes": [
                {"id": "figures", "type": "agent", "agent": "illustrator@1", "inputs": {}}
            ],
        }
    )
    report = deliver(run_dir, pipeline=pipeline, registry=registry, agents=agents)
    assert not report.ok
    assert "empty directory" in report.missing["figures"]
    assert not (run_dir / "output" / "figures").exists()


def test_a_dot_entry_alone_is_still_empty(tmp_path: Path) -> None:
    """Same rule as the gate: dot-entries are tooling, not content."""
    library = REPO_ROOT / "library"
    registry = ArtifactRegistry.load(library)
    run_dir = tmp_path / "run"
    d = run_dir / "steps" / "figures" / "main" / "output" / "illustration"
    d.mkdir(parents=True)
    (d / ".keep").write_text("", encoding="utf-8")

    agents = {
        "illustrator@1": agent_spec(
            "illustrator",
            consumes=[{"port": "article", "type": "article@v1"}],
            produces=[{"port": "illustration", "type": "illustration@v1"}],
        )
    }
    pipeline = Pipeline.model_validate(
        {
            "version": "0.1",
            "name": "p",
            "outputs": {"figures": "figures.illustration"},
            "nodes": [
                {"id": "figures", "type": "agent", "agent": "illustrator@1", "inputs": {}}
            ],
        }
    )
    assert "empty directory" in deliver(
        run_dir, pipeline=pipeline, registry=registry, agents=agents
    ).missing["figures"]


def test_an_empty_file_is_missing_too(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    out = run_dir / "steps" / "write" / "main" / "output"
    out.mkdir(parents=True)
    (out / "doc.md").write_text("", encoding="utf-8")
    report = deliver(
        run_dir,
        pipeline=_pipeline({"requirements": "write.doc"}),
        registry=write_registry(tmp_path),
        agents=_agents(),
    )
    assert "empty file" in report.missing["requirements"]
