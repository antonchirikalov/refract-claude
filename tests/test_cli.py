"""Tests for the CLI impl functions (SPEC §14) + a Phase-0 end-to-end golden test.

All tests use MockRuntime only -- no network, no ``~/.refract``, no real
the CLI. ``AppConfig`` is built in-process; the demo project is copied into
``tmp_path`` before each run so we never write into the real
``examples/demo-project``.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from refract.cli import (
    ActiveRunConflict,
    AppConfig,
    UsageError,
    _active_run,
    explain_impl,
    render_status,
    resolve_project,
    resume_impl,
    run_impl,
    status_impl,
    validate_impl,
)
from refract.explain import diagnose
from refract.models.config import ProvidersFile
from refract.models.ledger import RunStatus
from refract.models.pipeline import Pipeline
from refract.runtime.base import AgentRuntime, EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = REPO_ROOT / "library"
DEMO_PROJECT = REPO_ROOT / "examples" / "demo-project"

REQ = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"


def _clock_seq() -> Callable[[], str]:
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


def _app(
    *, with_key: bool = True, monkeypatch: pytest.MonkeyPatch | None = None
) -> AppConfig:
    if monkeypatch is not None and with_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    providers = ProvidersFile.model_validate(
        {
            "providers": {
                "claude": {"api_key_env": "ANTHROPIC_API_KEY", "max_concurrent": 4}
            }
        }
    )
    return AppConfig(library_path=LIBRARY_PATH, providers=providers)


def _copy_demo_project(tmp_path: Path) -> Path:
    # runs/ is skipped: a developer's real run in examples/ (live CLI,
    # node_modules, files still open) otherwise breaks the copy.
    dest = tmp_path / "demo-project"
    shutil.copytree(DEMO_PROJECT, dest, ignore=shutil.ignore_patterns("runs"))
    return dest


def _mock_runtime_factory(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
    return MockRuntime({"write:*": [ScriptedResponse(files={"requirements.md": REQ})]})


class _NoRunRuntime:
    """A runtime that fails the test if any step is actually executed."""

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        raise AssertionError(f"step {spec.step_id} should not be re-executed")

    async def close(self) -> None:
        return None


# --- 1. validate: happy path --------------------------------------------------


class TestValidateOk:
    def test_valid_demo_pipeline_returns_exit_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14: validate returns 0 for a valid project+pipeline.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)
        code = validate_impl(project, app=app)
        assert code == 0


# --- 2. validate: invalid cases -----------------------------------------------


class TestValidateInvalid:
    def test_provider_unavailable_yields_exit_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14/§8.3: E_PROVIDER_UNAVAILABLE is blocking when the provider's
        # api_key_env is unset.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        project = _copy_demo_project(tmp_path)
        app = _app(with_key=False)
        code = validate_impl(project, app=app)
        assert code == 2

    def test_missing_pipeline_selector_with_multiple_pipelines_is_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14: --pipeline is required when pipelines/ holds >1 file.
        project = _copy_demo_project(tmp_path)
        shutil.copyfile(
            project / "pipelines" / "demo.yaml", project / "pipelines" / "demo2.yaml"
        )
        app = _app(monkeypatch=monkeypatch)
        with pytest.raises(UsageError):
            validate_impl(project, app=app)


# --- 3. resolve_project / UsageError on missing project.yaml -----------------


class TestResolveProject:
    def test_missing_project_yaml_raises_usage_error(self, tmp_path: Path) -> None:
        # SPEC §14: a project dir without project.yaml is a usage error.
        empty = tmp_path / "not-a-project"
        empty.mkdir()
        with pytest.raises(UsageError):
            resolve_project(empty, None)


class TestProjectReferencesATemplate:
    """SPEC §7: a project differing only by subject references the library template."""

    LIBRARY = Path(__file__).resolve().parent.parent / "library"

    def _project(self, tmp_path: Path, body: str) -> Path:
        project = tmp_path / "proj"
        (project / "input").mkdir(parents=True)
        (project / "input" / "brief.md").write_text("Тема.", encoding="utf-8")
        (project / "project.yaml").write_text(body, encoding="utf-8")
        return project

    def test_named_template_resolves_to_the_library_file(self, tmp_path: Path) -> None:
        project = self._project(
            tmp_path,
            'version: "0.1"\nname: p\npipeline: explainer_article\n',
        )
        proj = resolve_project(
            project, None, library_path=self.LIBRARY, home=tmp_path / "home"
        )
        assert proj.pipeline_name == "explainer_article"
        assert proj.pipeline_path == self.LIBRARY / "templates" / "explainer_article.yaml"

    def test_no_copy_is_needed_in_the_project(self, tmp_path: Path) -> None:
        """The point of the feature: the project is a brief and a name, nothing else."""
        project = self._project(
            tmp_path,
            'version: "0.1"\nname: p\npipeline: explainer_article\n',
        )
        assert not (project / "pipelines").exists()
        resolve_project(project, None, library_path=self.LIBRARY, home=tmp_path / "home")

    def test_both_a_template_and_local_files_is_refused(self, tmp_path: Path) -> None:
        """Not resolved by precedence: which is meant is what the author must say."""
        project = self._project(
            tmp_path,
            'version: "0.1"\nname: p\npipeline: explainer_article\n',
        )
        (project / "pipelines").mkdir()
        (project / "pipelines" / "mine.yaml").write_text("x", encoding="utf-8")
        with pytest.raises(UsageError, match="not both"):
            resolve_project(
                project, None, library_path=self.LIBRARY, home=tmp_path / "home"
            )

    def test_unknown_template_names_what_exists(self, tmp_path: Path) -> None:
        project = self._project(tmp_path, 'version: "0.1"\nname: p\npipeline: nosuch\n')
        with pytest.raises(UsageError, match="explainer_article"):
            resolve_project(
                project, None, library_path=self.LIBRARY, home=tmp_path / "home"
            )

    def test_pipeline_flag_contradicting_the_reference_is_refused(
        self, tmp_path: Path
    ) -> None:
        project = self._project(
            tmp_path,
            'version: "0.1"\nname: p\npipeline: explainer_article\n',
        )
        with pytest.raises(UsageError, match="contradicts"):
            resolve_project(
                project, "research", library_path=self.LIBRARY, home=tmp_path / "home"
            )

    def test_neither_a_template_nor_a_local_pipeline_says_both_ways(
        self, tmp_path: Path
    ) -> None:
        project = self._project(tmp_path, 'version: "0.1"\nname: p\n')
        with pytest.raises(UsageError, match="templates"):
            resolve_project(
                project, None, library_path=self.LIBRARY, home=tmp_path / "home"
            )

    def test_a_local_pipeline_still_works(self, tmp_path: Path) -> None:
        """A project that genuinely forks keeps its own file, as before."""
        project = self._project(tmp_path, 'version: "0.1"\nname: p\n')
        (project / "pipelines").mkdir()
        shutil.copy(
            self.LIBRARY / "templates" / "explainer_article.yaml",
            project / "pipelines" / "mine.yaml",
        )
        proj = resolve_project(
            project, None, library_path=self.LIBRARY, home=tmp_path / "home"
        )
        assert proj.pipeline_name == "mine"


# --- 4. run: happy path --------------------------------------------------------


class TestRunHappyPath:
    def test_demo_project_runs_to_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14/§10.3/§10.5: run the demo pipeline (scanner -> map(2) ->
        # writer) on MockRuntime end to end.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)

        status, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )

        assert status is RunStatus.completed
        assert (run_dir / "state.json").exists()
        assert (run_dir / "events.jsonl").exists()

        manifest = json.loads(
            (
                run_dir
                / "steps"
                / "write"
                / "_out"
                / "requirements"
                / "_collection.json"
            ).read_text("utf-8")
        )
        assert manifest["stats"]["ok"] == 2

        text = render_status(run_dir)
        assert "scan" in text
        assert "write" in text
        assert "done" in text

    def test_run_streams_step_progress_to_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # SPEC §14: progress goes to stdout while the run is in flight — a real
        # run is minutes of LLM calls and must not look hung.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)

        run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )

        out = capsys.readouterr().out
        assert "-> scan" in out  # step started
        assert "ok scan" in out  # step finished
        assert "node write: done" in out  # node assembled
        assert out.isascii()  # Windows consoles are not always UTF-8


# --- 5. run: active-run lock conflict -----------------------------------------


class TestRunActiveLockConflict:
    def test_active_lock_raises_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §16.1: only one active run per project; a live lock blocks a
        # second run_impl call.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)

        other_run_dir = project / "runs" / "run_other"
        other_run_dir.mkdir(parents=True)
        (other_run_dir / ".active.lock").write_text(str(os.getpid()), encoding="utf-8")

        with pytest.raises(ActiveRunConflict) as excinfo:
            run_impl(
                project,
                app=app,
                runtime_factory=_mock_runtime_factory,
                run_id="run_test",
                clock=_clock_seq(),
            )
        assert excinfo.value.run_id == "run_other"

    def test_active_run_helper_reports_live_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _copy_demo_project(tmp_path)
        runs_dir = project / "runs"
        run_dir = runs_dir / "run_x"
        run_dir.mkdir(parents=True)
        (run_dir / ".active.lock").write_text(str(os.getpid()), encoding="utf-8")
        assert _active_run(runs_dir) == "run_x"


# --- 6. status: render_status --------------------------------------------------


class TestStatus:
    def test_render_status_after_completed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §14/§7: status reads state.json only and shows run/pipeline/nodes.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)
        _, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )

        text = render_status(run_dir)
        assert "run_test" in text
        assert "demo" in text
        assert "completed" in text
        assert "scan" in text
        assert "write" in text

        code = status_impl(run_dir)
        assert code == 0


class TestExplain:
    def test_explain_after_a_real_run_reports_cost_and_reads_gate_measures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SPEC §14: the post-mortem runs over a real run dir, ledger + events only."""
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)

        def paying_runtime(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
            return MockRuntime(
                {
                    "write:*": [
                        ScriptedResponse(
                            files={"requirements.md": REQ},
                            usage={
                                "cost": 0.125,
                                "tokens": {"input_tokens": 500, "output_tokens": 90},
                                "duration_ms": 2000,
                            },
                        )
                    ]
                }
            )

        _, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=paying_runtime,
            run_id="run_explain",
            clock=_clock_seq(),
        )

        diagnosis = diagnose(run_dir)
        # two map elements, each one paid call
        assert diagnosis.total.calls == 2
        assert diagnosis.total.cost_usd == pytest.approx(0.25)
        assert diagnosis.total.input_tokens == 1000
        assert diagnosis.root_cause is None
        assert diagnosis.wasted.cost_usd == pytest.approx(0.0)
        # the gate wrote its measurements, so the post-mortem can read them
        report = json.loads(
            (run_dir / "steps" / "write" / "alpha-txt" / "gate_report.json").read_text(
                "utf-8"
            )
        )
        assert report["ports"][0]["measures"]["chars"] == len(REQ)

        assert explain_impl(run_dir) == 0
        assert explain_impl(run_dir, as_json=True) == 0

    def test_explain_without_a_ledger_is_a_usage_error(self, tmp_path: Path) -> None:
        with pytest.raises(UsageError):
            explain_impl(tmp_path / "not-a-run")


# --- 7. resume: done nodes are not re-executed --------------------------------


class TestResume:
    def test_resume_after_completion_does_not_rerun_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §10.5/§9: resume seeds from the ledger; a fully-done run resumes
        # to `completed` without invoking run_step again.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)
        _, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )

        def no_run_factory(app: AppConfig, pipeline: Pipeline) -> AgentRuntime:
            return _NoRunRuntime()

        status = resume_impl(
            run_dir,
            app=app,
            runtime_factory=no_run_factory,
            clock=_clock_seq(),
        )
        assert status is RunStatus.completed

    def test_resume_refuses_when_another_run_is_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §16.1: one active run per project — resume must refuse if a
        # sibling run in the same project holds a live .active.lock.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)
        _, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )
        # a second, "live" run in the same project (our own pid → alive)
        other = run_dir.parent / "run_other"
        other.mkdir()
        (other / ".active.lock").write_text(str(os.getpid()), encoding="utf-8")

        with pytest.raises(ActiveRunConflict):
            resume_impl(run_dir, app=app, runtime_factory=_mock_runtime_factory)

    def test_force_step_resets_step_node_and_downstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §10.5: --force-step archives the step and resets its node AND all
        # downstream nodes to pending, so their outputs rebuild on resume.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)
        _, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_test",
            clock=_clock_seq(),
        )
        # force the scanner step; `write` is downstream of `scan`.
        status = resume_impl(
            run_dir,
            app=app,
            runtime_factory=_mock_runtime_factory,
            force_step="scan",
            clock=_clock_seq(),
        )
        assert status is RunStatus.completed
        # the forced step's dir was archived (attempts/1 holds the prior output)
        assert (run_dir / "steps" / "scan" / "main" / "attempts" / "1").is_dir()
        # downstream output collection is rebuilt and still complete
        manifest = json.loads(
            (
                run_dir
                / "steps"
                / "write"
                / "_out"
                / "requirements"
                / "_collection.json"
            ).read_text("utf-8")
        )
        assert manifest["stats"]["ok"] == 2


# --- 8. end-to-end golden test: full run-dir tree -----------------------------


class TestE2EGolden:
    def test_full_run_dir_tree_matches_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SPEC §9/§10.3/§10.5: snapshot, ledger, events and the output
        # collection manifest all match the expected shapes for a Phase-0 run.
        project = _copy_demo_project(tmp_path)
        app = _app(monkeypatch=monkeypatch)

        status, run_dir = run_impl(
            project,
            app=app,
            runtime_factory=_mock_runtime_factory,
            run_id="run_golden",
            clock=_clock_seq(),
        )
        assert status is RunStatus.completed

        # --- snapshot (§9) ---
        snap = run_dir / "snapshot"
        assert (snap / "pipeline.yaml").exists()
        assert (snap / "resolved.yaml").exists()
        assert (snap / "agents.lock.json").exists()
        assert (snap / "agents" / "demo_writer@1" / "agent.yaml").exists()

        lock = json.loads((snap / "agents.lock.json").read_text("utf-8"))
        assert "demo_writer@1" in lock
        assert lock["demo_writer@1"].startswith("sha256:")

        # --- state.json (§9) ---
        state = json.loads((run_dir / "state.json").read_text("utf-8"))
        assert state["status"] == "completed"
        assert state["nodes"]["scan"]["status"] == "done"
        assert state["nodes"]["write"]["status"] == "done"
        step_ids = set(state["steps"])
        assert step_ids == {"scan", "write:alpha-txt", "write:beta-txt"}
        for sid in step_ids:
            step = state["steps"][sid]
            assert step["status"] == "done"
            assert step["outcome"] == "ok"

        # --- events.jsonl: non-empty, append-only, increasing seq (§9) ---
        lines = (run_dir / "events.jsonl").read_text("utf-8").splitlines()
        assert len(lines) > 0
        records = [json.loads(line) for line in lines]
        seqs = [r["seq"] for r in records]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))

        # --- output collection manifest (§10.3) ---
        manifest = json.loads(
            (
                run_dir
                / "steps"
                / "write"
                / "_out"
                / "requirements"
                / "_collection.json"
            ).read_text("utf-8")
        )
        assert manifest["type"] == "collection<requirements@v1>"
        assert manifest["stats"] == {"total": 2, "ok": 2, "failed": 0}
        slugs = {item["slug"] for item in manifest["items"]}
        assert slugs == {"alpha-txt", "beta-txt"}
        for item in manifest["items"]:
            assert item["status"] == "ok"
            payload = (
                run_dir
                / "steps"
                / "write"
                / "_out"
                / "requirements"
                / item["slug"]
                / "requirements.md"
            )
            assert payload.exists()
            assert payload.read_text("utf-8") == REQ


# --- catalog (SPEC §19.1) ------------------------------------------------------


class TestCatalogCommand:
    def test_human_summary_lists_agents_and_builtins(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from refract.cli import catalog_impl

        assert catalog_impl(_app(monkeypatch=monkeypatch)) == 0

        out = capsys.readouterr().out
        assert "source_processor@1" in out
        assert "builtin/scanner" in out
        assert "collection<extract@v1>" in out  # port types are shown

    def test_json_output_is_the_whole_catalog(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from refract.cli import catalog_impl

        assert catalog_impl(_app(monkeypatch=monkeypatch), as_json=True) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["version"]
        assert payload["agents"] and payload["builtins"] and payload["constraints"]
