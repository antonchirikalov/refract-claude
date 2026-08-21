"""Tests for checkpoints — parking a run for human verification (SPEC §21).

The editorial flow this exists for: reach the requirements, stop, let a human read
(and even fix) the document, then continue. MockRuntime only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from refract.cli import (
    AppConfig,
    UsageError,
    render_status,
    resume_impl,
    run_impl,
    write_answer,
)
from refract.models.config import ProvidersFile
from refract.models.ledger import RunStatus
from refract.models.pipeline import Pipeline
from refract.runtime.mock import MockRuntime, ScriptedResponse

from reqdoc import requirements_doc

REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = REPO_ROOT / "library"
DEMO_PROJECT = REPO_ROOT / "examples" / "demo-project"

REQ = requirements_doc("Demo")


def _clock_seq() -> "callable":
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


def _app(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    providers = ProvidersFile.model_validate(
        {"providers": {"claude": {"api_key_env": "ANTHROPIC_API_KEY"}}}
    )
    return AppConfig(library_path=LIBRARY_PATH, providers=providers)


def _factory(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
    return MockRuntime({"write:*": [ScriptedResponse(files={"requirements.md": REQ})]})


def _project(tmp_path: Path, *, checkpoints: list[str] | None = None) -> Path:
    dest = tmp_path / "demo-project"
    shutil.copytree(DEMO_PROJECT, dest, ignore=shutil.ignore_patterns("runs"))
    if checkpoints is not None:
        path = dest / "pipelines" / "demo.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        data["checkpoints"] = checkpoints
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return dest


def _run(project: Path, app: AppConfig, **kw: object) -> tuple[RunStatus, Path]:
    return run_impl(
        project,
        app=app,
        runtime_factory=_factory,
        clock=_clock_seq(),
        **kw,  # type: ignore[arg-type]
    )


class TestParking:
    def test_run_parks_after_the_declared_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §21.2: the checkpoint fires AFTER the node is done and its outputs
        # are assembled, so the artifact exists and can be read.
        project = _project(tmp_path, checkpoints=["scan"])

        status, run_dir = _run(project, _app(monkeypatch))

        assert status is RunStatus.waiting_human
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["awaiting_checkpoint"] == "scan"
        assert state["nodes"]["scan"]["status"] == "done"
        # the rest of the graph did not run
        assert state["nodes"]["write"]["status"] == "pending"
        assert not any(k.startswith("write") for k in state["steps"])

        request = json.loads(
            (run_dir / "steps" / "scan" / "checkpoint" / "request.json").read_text(
                "utf-8"
            )
        )
        assert request["node"] == "scan"
        assert request["outputs"]  # points at what to review

    def test_status_tells_you_how_to_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, _app(monkeypatch))

        text = render_status(run_dir)

        assert "awaiting checkpoint: scan" in text
        assert "refract answer" in text
        assert "output:" in text

    def test_resume_without_a_decision_parks_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No silent bypass: resuming a parked run keeps it parked until a human
        # actually answers.
        app = _app(monkeypatch)
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, app)

        status = resume_impl(
            run_dir, app=app, runtime_factory=_factory, clock=_clock_seq()
        )

        assert status is RunStatus.waiting_human
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["awaiting_checkpoint"] == "scan"
        assert state["nodes"]["write"]["status"] == "pending"


class TestContinuing:
    def test_continue_lets_the_rest_of_the_graph_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch)
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, app)

        write_answer(run_dir, "scan", "continue")
        status = resume_impl(
            run_dir, app=app, runtime_factory=_factory, clock=_clock_seq()
        )

        assert status is RunStatus.completed
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["awaiting_checkpoint"] is None
        assert state["nodes"]["write"]["status"] == "done"

    def test_a_human_edit_between_park_and_resume_reaches_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The point of the checkpoint (SPEC §21.2 step 3): the reviewer may FIX the
        # output, and the downstream must see the fixed version, not a copy.
        app = _app(monkeypatch)
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, app)

        scanned = run_dir / "steps" / "scan" / "main" / "output" / "sources"
        edited = next(scanned.glob("*/*.txt"))
        edited.write_text("CORRECTED BY A HUMAN", encoding="utf-8")

        write_answer(run_dir, "scan", "continue")
        resume_impl(run_dir, app=app, runtime_factory=_factory, clock=_clock_seq())

        materialized = list((run_dir / "steps" / "write").glob("*/input/source/*.txt"))
        assert materialized, "the map step should have materialized its source"
        assert any(p.read_text("utf-8") == "CORRECTED BY A HUMAN" for p in materialized)

    def test_rejecting_a_checkpoint_stops_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(monkeypatch)
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, app)

        write_answer(run_dir, "scan", "reject")
        status = resume_impl(
            run_dir, app=app, runtime_factory=_factory, clock=_clock_seq()
        )

        assert status is RunStatus.cancelled
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["nodes"]["write"]["status"] != "done"


class TestRunScoped:
    def test_stop_after_needs_no_declaration_in_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)  # pipeline declares no checkpoints

        status, run_dir = _run(project, _app(monkeypatch), stop_after=["scan"])

        assert status is RunStatus.waiting_human
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["awaiting_checkpoint"] == "scan"

    def test_stop_after_is_recorded_in_the_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §21.1: a resume must honour the same checkpoints as the original run,
        # so the run-scoped set lives in the snapshot, not just in argv.
        project = _project(tmp_path)
        _, run_dir = _run(project, _app(monkeypatch), stop_after=["scan"])

        resolved = yaml.safe_load(
            (run_dir / "snapshot" / "resolved.yaml").read_text("utf-8")
        )

        assert resolved["checkpoints"] == ["scan"]

    def test_unknown_stop_after_node_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)

        with pytest.raises(UsageError, match="stop-after"):
            _run(project, _app(monkeypatch), stop_after=["nope"])


class TestNoDoubleExecution:
    def test_resuming_a_run_that_is_already_executing_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seen live: the UI answered a checkpoint and also called resume, so two
        # schedulers ran over one ledger — the second killed the first mid-node and
        # left the run failed with a node stuck at `running`. A live lock on THIS run
        # is now a conflict, not just a lock on a different run (§16.1).
        from refract.cli import ActiveRunConflict, _LOCK_NAME

        app = _app(monkeypatch)
        project = _project(tmp_path, checkpoints=["scan"])
        _, run_dir = _run(project, app)
        write_answer(run_dir, "scan", "continue")
        (run_dir / _LOCK_NAME).write_text(str(__import__("os").getpid()), "utf-8")

        with pytest.raises(ActiveRunConflict):
            resume_impl(run_dir, app=app, runtime_factory=_factory, clock=_clock_seq())
