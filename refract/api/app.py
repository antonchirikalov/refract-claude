"""FastAPI REST/WS API for the refract engine (SPEC §15).

The API is a thin adapter over the existing CLI ``*_impl`` functions — it does
NOT reimplement the engine. ``run_impl``/``resume_impl`` are synchronous (they
call ``asyncio.run`` internally), so runs are launched in the background via
``asyncio.create_task(asyncio.to_thread(...))`` and the API returns the
engine-generated ``run_id`` immediately (202). ``GET /api/runs/{id}`` reads the
run's ``state.json`` (I7 — CLI/UI render only ``state.json`` + ``events.jsonl``).

Errors map to HTTP: ``ValidationFailed`` → 422, ``ActiveRunConflict`` → 409,
``UsageError`` → 400, missing project/run → 404. Provider API-key *values* are
never echoed (I8); only the env-var name + availability flag are exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from refract.cli import (
    ActiveRunConflict,
    AppConfig,
    RuntimeFactory,
    UsageError,
    _active_run,
    _default_runtime_factory,
    _new_run_id,
    resolve_project,
    refract_home,
    resume_impl,
    run_impl,
    write_answer,
)
from refract.catalog import build_catalog
from refract.events import EVENTS_FILENAME, utcnow_iso
from refract.graph import LoadedGraph, load_agents, load_pipeline
from refract.models.ledger import RunState
from refract.models.pipeline import (
    AgentNode,
    DiscoverNode,
    LoopNode,
    Node,
    SelectNode,
)
from refract.patch import apply_node_patch
from refract.registry import ArtifactRegistry
from refract.state import read_state, step_workdir
from refract.templates_lib import (
    TEMPLATES_SUBDIR,
    find_template,
    list_templates,
    template_metadata,
)

# Statuses at which no further events flow until the client acts — the WS closes.
# waiting_human is "paused for an answer", not truly finished, but no events come
# until a resume, so the events socket closes on it too.
_TERMINAL = {"completed", "failed", "cancelled", "waiting_human"}

_log = logging.getLogger("refract.api")


# --- request / response models ----------------------------------------------


class CreateProjectRequest(BaseModel):
    """New project: optionally from a template, pointed at a documents folder.

    ``input`` may live anywhere — the documents folder is referenced, never copied
    (SPEC-UI §2). ``model`` sets ``defaults.model`` for the project.
    """

    name: str
    template: str | None = None
    input: str | None = None
    model: str | None = None


class ImportDocumentsRequest(BaseModel):
    """Copy documents from a folder into the project's input (SPEC-UI §4)."""

    path: str
    replace: bool = False  # clear the project's input folder first


class BriefRequest(BaseModel):
    """The research brief a ``input_mode: brief`` pipeline reads (SPEC-UI §4)."""

    text: str


class InputSummary(BaseModel):
    input_dir: str
    external: bool  # True when input points outside the project (referenced folder)
    entries: list[str]


class NodePatch(BaseModel):
    """A scoped node edit from the UI inspector (SPEC §19.2.1)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    unset_model: bool = False  # explicit, since `model: null` cannot say "remove"
    # ``body``/``critic``/``selector``, or ``body1``..``bodyN`` for a chain element
    # (SPEC §10.3); the exact set depends on the node, so patch.py judges it
    block: str | None = None
    params: dict[str, Any] | None = None


class SaveTemplateRequest(BaseModel):
    """Save a project's pipeline as a user template (SPEC-UI §5)."""

    name: str
    from_project: str
    pipeline: str


class RunSummary(BaseModel):
    run_id: str
    status: str
    pipeline: str
    created_at: str
    finished_at: str | None = None
    # set when the run is parked at a checkpoint (SPEC §21) — lets a project list
    # show "parked at <node>, continue here" without opening the run
    awaiting_checkpoint: str | None = None


class ValidationError(BaseModel):
    code: str
    node_id: str | None = None
    message: str


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[ValidationError] = Field(default_factory=list)


class StartRunRequest(BaseModel):
    pipeline: str
    overrides: dict[str, str] | None = None
    reuse_from: str | None = None
    force: list[str] | None = None
    stop_after: list[str] | None = None  # extra checkpoints for this run (SPEC §21)


class StartRunResponse(BaseModel):
    run_id: str


class AnswerRequest(BaseModel):
    step_id: str
    answer: str


class PipelineText(BaseModel):
    name: str
    yaml: str
    hash: str = ""  # sha256 of the text — the client's next base_hash (§19.2)


class PipelineWriteResponse(BaseModel):
    """Result of a pipeline write (SPEC §19.2)."""

    name: str
    committed: bool
    hash: str  # sha256 of what is now on disk
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[ValidationError] = Field(default_factory=list)


class GraphBlock(BaseModel):
    """One agent inside a meta-node — a loop's body/critic, a select's selector."""

    role: str  # body | critic | selector
    agent: str
    model: str | None = None


class GraphNode(BaseModel):
    id: str
    type: str
    agents: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    fan_out: str | None = None  # "map" | "map_over" | None
    # effective models (resolved like the snapshot does, §7): one per node, several
    # for map_over — the UI badges the provider so you can see what runs where
    models: list[str] = Field(default_factory=list)
    # a meta-node's inner agents, so the UI can draw it as a container (SPEC-UI §4)
    blocks: list[GraphBlock] = Field(default_factory=list)
    # the models a select is choosing between (its candidates' producer, §10.3)
    candidate_models: list[str] = Field(default_factory=list)
    # a short, renderable summary of what shapes the node: rounds, fallback, workers
    facts: dict[str, str] = Field(default_factory=dict)
    checkpoint: bool = False


class GraphEdge(BaseModel):
    source: str
    target: str
    port: str


class PipelineGraph(BaseModel):
    """A pipeline's shape for the UI (SPEC-UI §4) — derived, never authored."""

    name: str
    input_mode: str
    checkpoints: list[str]  # nodes after which the run parks for review (§21)
    order: list[str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ProviderInfo(BaseModel):
    name: str
    # None for a provider served by the Claude Code CLI: it authenticates from its own
    # subscription login, so there is no key variable to name
    api_key_env: str | None = None
    available: bool
    max_concurrent: int
    # model-ids offered under this provider (SPEC §7); the UI needs them to build a
    # "provider/model-id" string, which is what a project's defaults.model is
    models: list[str] = Field(default_factory=list)


class FsEntry(BaseModel):
    name: str
    is_dir: bool


# --- in-memory run registry --------------------------------------------------


@dataclass
class RunRecord:
    run_dir: Path
    project_id: str
    task: asyncio.Task[Any] | None = None
    status: str = "running"
    error: str | None = None


@dataclass
class _State:
    projects_root: Path
    app_config: AppConfig
    runtime_factory: RuntimeFactory
    clock: Callable[[], str]
    runs: dict[str, RunRecord] = field(default_factory=dict)


def create_app(
    *,
    projects_root: Path,
    app_config: AppConfig,
    runtime_factory: RuntimeFactory | None = None,
    clock: Callable[[], str] = utcnow_iso,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the refract REST/WS API app (SPEC §15).

    ``projects_root`` holds one directory per project (each with a
    ``project.yaml``). ``runtime_factory`` defaults to the real Claude Code
    runtime; tests inject a MockRuntime factory. ``static_dir`` (the built SPA)
    is served at the root when given, so one process serves API and UI.
    """
    st = _State(
        projects_root=Path(projects_root),
        app_config=app_config,
        runtime_factory=runtime_factory or _default_runtime_factory,
        clock=clock,
    )
    api = FastAPI(title="refract", version="0.2")

    # --- helpers -------------------------------------------------------------

    def _project_dir(project_id: str) -> Path:
        pdir = st.projects_root / project_id
        if not (pdir / "project.yaml").exists():
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        return pdir

    def _find_run_dir(run_id: str) -> Path:
        rec = st.runs.get(run_id)
        if rec is not None and (rec.run_dir / "state.json").exists():
            return rec.run_dir
        # fall back to scanning projects_root/*/runs/{run_id}
        for pdir in st.projects_root.iterdir():
            candidate = pdir / "runs" / run_id
            if (candidate / "state.json").exists():
                return candidate
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")

    def _validate_text(
        project_dir: Path, name: str, text: str
    ) -> tuple[list[ValidationError], list[ValidationError]]:
        """Validate pipeline TEXT without touching the project's file (§19.2).

        The candidate is written to a temp dir and loaded from there, so a failed
        write never leaves a half-valid pipeline behind and a concurrent request
        cannot see a scratch file in ``pipelines/``.
        """
        from refract.cli import _build_context
        from refract.models.config import ProjectConfig

        raw = yaml.safe_load((project_dir / "project.yaml").read_text("utf-8")) or {}
        config = ProjectConfig.model_validate(raw)
        registry = ArtifactRegistry.load(st.app_config.library_path)
        agents, agent_errors = load_agents(st.app_config.library_path)
        ctx = _build_context(
            st.app_config,
            registry=registry,
            agents=agents,
            default_model=config.defaults.model,
            model_overrides={},
        )
        with tempfile.TemporaryDirectory() as td:
            candidate = Path(td) / f"{name}.yaml"
            candidate.write_text(text, encoding="utf-8")
            graph = load_pipeline(candidate, ctx)
        errors: list[ValidationError] = []
        warnings: list[ValidationError] = []
        for e in list(agent_errors) + list(graph.errors):
            item = ValidationError(
                code=getattr(e.code, "value", str(e.code)),
                node_id=getattr(e, "node_id", None),
                message=getattr(e, "message", str(e)),
            )
            if getattr(e.code, "is_warning", False):
                warnings.append(item)
            else:
                errors.append(item)
        return errors, warnings

    def _load_graph(project_dir: Path, path: Path) -> "LoadedGraph":
        from refract.cli import _build_context
        from refract.models.config import ProjectConfig

        raw = yaml.safe_load((project_dir / "project.yaml").read_text("utf-8")) or {}
        config = ProjectConfig.model_validate(raw)
        agents, _ = load_agents(st.app_config.library_path)
        ctx = _build_context(
            st.app_config,
            registry=ArtifactRegistry.load(st.app_config.library_path),
            agents=agents,
            default_model=config.defaults.model,
            model_overrides={},
        )
        return load_pipeline(path, ctx)

    def _collect_errors(
        project_dir: Path, pipeline: str, overrides: dict[str, str]
    ) -> list[ValidationError]:
        proj = resolve_project(project_dir, pipeline)
        registry = ArtifactRegistry.load(st.app_config.library_path)
        agents, agent_errors = load_agents(st.app_config.library_path)
        from refract.cli import _build_context

        ctx = _build_context(
            st.app_config,
            registry=registry,
            agents=agents,
            default_model=proj.config.defaults.model,
            model_overrides=overrides,
        )
        graph = load_pipeline(proj.pipeline_path, ctx)
        out: list[ValidationError] = []
        for e in list(agent_errors) + list(graph.errors):
            if getattr(e.code, "is_warning", False):
                continue
            code = getattr(e.code, "value", str(e.code))
            out.append(
                ValidationError(
                    code=code,
                    node_id=getattr(e, "node_id", None),
                    message=getattr(e, "message", str(e)),
                )
            )
        return out

    def _artifact_base(run_dir: Path, step_id: str) -> Path:
        """Resolve a step's (or node's) output dir for artifact listing.

        A step id maps to its workdir via the engine's own naming
        (:func:`step_workdir`); a NODE id additionally resolves to the assembled
        ``_out`` of a map/loop/select/discover node, which is what the UI links to
        for a checkpoint.
        """
        out = step_workdir(run_dir, step_id) / "output"
        if out.is_dir():
            return out
        assembled = run_dir / "steps" / step_id / "_out"
        if assembled.is_dir():
            return assembled
        raise HTTPException(
            status_code=404, detail=f"no artifacts for step {step_id!r}"
        )

    # --- projects ------------------------------------------------------------

    @api.get("/api/projects")
    def list_projects() -> list[str]:
        if not st.projects_root.is_dir():
            return []
        return sorted(
            p.name for p in st.projects_root.iterdir() if (p / "project.yaml").exists()
        )

    @api.post("/api/projects", status_code=201)
    def create_project(req: CreateProjectRequest) -> dict[str, str]:
        """Create a project, optionally from a template (SPEC-UI §5).

        The documents folder is referenced, not copied: ``input`` goes into
        ``project.yaml`` as given, so it may point anywhere on disk. Without it the
        project gets its own empty ``input/``.
        """
        name = req.name.strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status_code=400, detail=f"bad project name {name!r}")
        pdir = st.projects_root / name
        if pdir.exists():
            raise HTTPException(status_code=409, detail=f"project {name!r} exists")

        pipeline_text: str | None = None
        if req.template is not None:
            ref = find_template(
                req.template, st.app_config.library_path, refract_home()
            )
            if ref is None:
                available = ", ".join(
                    t.name
                    for t in list_templates(st.app_config.library_path, refract_home())
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown template {req.template!r} (available: {available})",
                )
            pipeline_text = ref.path.read_text("utf-8")

        (pdir / "pipelines").mkdir(parents=True)
        if pipeline_text is not None:
            (pdir / "pipelines" / f"{req.template}.yaml").write_text(
                pipeline_text, encoding="utf-8"
            )
        config: dict[str, Any] = {"version": "0.1", "name": name}
        if req.input:
            config["input"] = req.input
        else:
            (pdir / "input").mkdir()
            config["input"] = "./input"
        if req.model:
            config["defaults"] = {"model": req.model}
        (pdir / "project.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return {"id": name}

    @api.get("/api/projects/{project_id}/runs")
    def list_project_runs(project_id: str) -> list[RunSummary]:
        """Runs of a project, newest first — read from each ledger (I7)."""
        pdir = _project_dir(project_id)
        runs_dir = pdir / "runs"
        out: list[RunSummary] = []
        for candidate in (
            sorted(
                (p for p in runs_dir.iterdir() if (p / "state.json").exists()),
                reverse=True,
            )
            if runs_dir.is_dir()
            else []
        ):
            state = RunState.model_validate(read_state(candidate))
            out.append(
                RunSummary(
                    run_id=state.run_id,
                    status=state.status.value,
                    pipeline=state.pipeline,
                    created_at=state.created_at,
                    finished_at=state.finished_at,
                    awaiting_checkpoint=state.awaiting_checkpoint,
                )
            )
        return out

    # --- project input (documents / brief) -----------------------------------

    def _input_dir(project_dir: Path) -> Path:
        from refract.models.config import ProjectConfig

        raw = yaml.safe_load((project_dir / "project.yaml").read_text("utf-8")) or {}
        config = ProjectConfig.model_validate(raw)
        return (project_dir / config.input).resolve()

    @api.get("/api/projects/{project_id}/input")
    def get_input(project_id: str) -> InputSummary:
        """What the pipeline will scan: the folder and its top-level entries."""
        pdir = _project_dir(project_id)
        target = _input_dir(pdir)
        entries = (
            sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
            if target.is_dir()
            else []
        )
        return InputSummary(
            input_dir=str(target),
            external=pdir.resolve() not in target.parents
            and target != (pdir / "input").resolve(),
            entries=entries,
        )

    @api.post("/api/projects/{project_id}/input/documents")
    def import_documents(project_id: str, req: ImportDocumentsRequest) -> InputSummary:
        """Copy a folder's documents INTO the project (SPEC-UI §4).

        The alternative — referencing an outside folder via ``project.yaml: input:``
        — stays available at project creation. Copying makes the project
        self-contained: the sources cannot move or change under a finished run.
        """
        pdir = _project_dir(project_id)
        source = Path(req.path)
        if not source.is_dir():
            raise HTTPException(status_code=400, detail=f"not a folder: {req.path}")
        target = _input_dir(pdir)
        if req.replace and target.is_dir():
            for existing in target.iterdir():
                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()
        target.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir()):
            if entry.name.startswith("."):  # tooling artifacts are not sources (§13)
                continue
            destination = target / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, dirs_exist_ok=True)
            else:
                shutil.copyfile(entry, destination)
        return get_input(project_id)

    @api.put("/api/projects/{project_id}/input/brief")
    def put_brief(project_id: str, req: BriefRequest) -> InputSummary:
        """Store a research brief as the project's single source document.

        A ``brief`` pipeline needs no special engine path — the brief is written to
        ``input/brief.md`` and scanned like any other document (SPEC §8 input_mode).
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="brief is empty")
        pdir = _project_dir(project_id)
        target = _input_dir(pdir)
        target.mkdir(parents=True, exist_ok=True)
        _atomic_write(target / "brief.md", text + "\n")
        return get_input(project_id)

    # --- templates -----------------------------------------------------------

    @api.get("/api/templates")
    def list_templates_endpoint() -> list[dict[str, Any]]:
        """Template gallery: shipped + user templates with derived metadata."""
        agents, _ = load_agents(st.app_config.library_path)
        return [
            template_metadata(ref, agents=agents)
            for ref in list_templates(st.app_config.library_path, refract_home())
        ]

    @api.post("/api/templates", status_code=201)
    def save_template(req: SaveTemplateRequest) -> dict[str, str]:
        """Save a project's pipeline as a user template (SPEC-UI §5)."""
        name = req.name.strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status_code=400, detail=f"bad template name {name!r}")
        pdir = _project_dir(req.from_project)
        source = pdir / "pipelines" / f"{req.pipeline}.yaml"
        if not source.exists():
            raise HTTPException(
                status_code=404, detail=f"no pipeline {req.pipeline!r} in that project"
            )
        existing = find_template(name, st.app_config.library_path, refract_home())
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"template {name!r} already exists ({existing.source})",
            )
        target_dir = refract_home() / TEMPLATES_SUBDIR
        target_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(target_dir / f"{name}.yaml", _renamed(source, name))
        return {"name": name, "source": "user"}

    def _renamed(source: Path, name: str) -> str:
        """The pipeline text with its ``name:`` set to the template's name.

        The gallery titles a template by the pipeline's own ``name`` field, so leaving
        it alone showed the saved template under the source project's pipeline name —
        a user who saved "erp-discovery" found a card called "chain". Round-tripped
        through ruamel so comments (which become the gallery description) survive.
        """
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        yaml_rt.width = 4096
        doc = yaml_rt.load(source.read_text("utf-8"))
        if isinstance(doc, dict):
            doc["name"] = name
        buffer = io.StringIO()
        yaml_rt.dump(doc, buffer)
        return buffer.getvalue()

    @api.get("/api/projects/{project_id}/pipelines")
    def list_pipelines(project_id: str) -> list[str]:
        pdir = _project_dir(project_id)
        pipelines_dir = pdir / "pipelines"
        if not pipelines_dir.is_dir():
            return []
        return sorted(p.stem for p in pipelines_dir.glob("*.yaml"))

    @api.get("/api/projects/{project_id}/pipelines/{name}")
    def get_pipeline(project_id: str, name: str) -> PipelineText:
        pdir = _project_dir(project_id)
        path = pdir / "pipelines" / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no pipeline {name!r}")
        text = path.read_text("utf-8")
        return PipelineText(name=name, yaml=text, hash=_text_hash(text))

    @api.put("/api/projects/{project_id}/pipelines/{name}")
    def put_pipeline(
        project_id: str,
        name: str,
        body: str = Body(..., media_type="text/plain"),
        allow_invalid: bool = Query(False),
        base_hash: str | None = Query(None),
    ) -> PipelineWriteResponse:
        """Replace a pipeline: verify, then commit atomically (SPEC §19.2).

        Blocking validation errors mean nothing is written — the editor gets the
        full report back. ``allow_invalid`` saves the draft anyway (and still
        reports). ``base_hash`` is optimistic locking against the text the client
        read; omit it and no such check happens.
        """
        pdir = _project_dir(project_id)
        active = _active_run(pdir / "runs")
        if active is not None:
            raise HTTPException(
                status_code=409, detail=f"project has an active run ({active})"
            )
        path = pdir / "pipelines" / f"{name}.yaml"
        if base_hash is not None and _text_hash(_read_or_empty(path)) != base_hash:
            raise HTTPException(
                status_code=409,
                detail="stale base_hash: the pipeline changed since you read it",
            )

        errors, warnings = _validate_text(pdir, name, body)
        if errors and not allow_invalid:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "pipeline not written: validation failed",
                    "errors": [e.model_dump() for e in errors],
                    "warnings": [w.model_dump() for w in warnings],
                },
            )
        path.parent.mkdir(exist_ok=True)
        _atomic_write(path, body)
        return PipelineWriteResponse(
            name=name,
            committed=True,
            hash=_text_hash(body),
            errors=errors,
            warnings=warnings,
        )

    @api.patch("/api/projects/{project_id}/pipelines/{name}/nodes/{node_id}")
    def patch_node(
        project_id: str,
        name: str,
        node_id: str,
        patch: NodePatch,
        base_hash: str | None = Query(None),
    ) -> PipelineWriteResponse:
        """Set a node's model or params, keeping the file's comments (SPEC §19.2.1).

        Deliberately narrow: the general patch vocabulary was rejected (§19.2), but
        the inspector needs exactly two things — which model runs a node and how many
        rounds a loop takes. Applied to a round-trip document, validated in full, and
        committed only if the result is valid.
        """
        pdir = _project_dir(project_id)
        active = _active_run(pdir / "runs")
        if active is not None:
            raise HTTPException(
                status_code=409, detail=f"project has an active run ({active})"
            )
        path = pdir / "pipelines" / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no pipeline {name!r}")
        current = path.read_text("utf-8")
        if base_hash is not None and _text_hash(current) != base_hash:
            raise HTTPException(
                status_code=409,
                detail="stale base_hash: the pipeline changed since you read it",
            )
        try:
            updated = apply_node_patch(
                current,
                node_id=node_id,
                model=patch.model,
                unset_model=patch.unset_model,
                block=patch.block,
                params=patch.params,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValueError, PydanticValidationError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        errors, warnings = _validate_text(pdir, name, updated)
        if errors:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "not written: the edit makes the pipeline invalid",
                    "errors": [e.model_dump() for e in errors],
                    "warnings": [w.model_dump() for w in warnings],
                },
            )
        _atomic_write(path, updated)
        return PipelineWriteResponse(
            name=name,
            committed=True,
            hash=_text_hash(updated),
            errors=errors,
            warnings=warnings,
        )

    @api.get("/api/catalog")
    def get_catalog() -> dict[str, Any]:
        """The authoring catalog: what a pipeline can be built from (SPEC §19.1)."""
        return build_catalog(st.app_config.library_path)

    @api.get("/api/projects/{project_id}/pipelines/{name}/graph")
    def get_pipeline_graph(project_id: str, name: str) -> PipelineGraph:
        """Nodes + edges of a pipeline, for drawing it (SPEC-UI §4).

        Derived by the engine's own loader, so a client never parses YAML and can
        never disagree with the validator about the graph's shape.
        """
        pdir = _project_dir(project_id)
        path = pdir / "pipelines" / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no pipeline {name!r}")
        graph = _load_graph(pdir, path)
        if graph.pipeline is None:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "code": getattr(e.code, "value", str(e.code)),
                        "message": e.message,
                    }
                    for e in graph.errors
                ],
            )
        agents, _ = load_agents(st.app_config.library_path)
        raw_config = yaml.safe_load((pdir / "project.yaml").read_text("utf-8")) or {}
        from refract.models.config import ProjectConfig
        from refract.snapshot import build_resolved

        resolved = build_resolved(
            graph.pipeline,
            agents=agents,
            overrides={},
            default_model=ProjectConfig.model_validate(raw_config).defaults.model,
        )
        resolved_nodes = resolved["nodes"]
        assert isinstance(resolved_nodes, list)
        models_by_node = {
            str(n["id"]): _node_models(n) for n in resolved_nodes if isinstance(n, dict)
        }
        checkpoints = set(graph.pipeline.checkpoints)
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        for node in graph.pipeline.nodes:
            refs = _node_agent_refs(node)
            needs: list[str] = []
            for ref in refs:
                spec = agents.get(ref)
                for cap in spec.needs if spec else []:
                    if cap not in needs:
                        needs.append(cap)
            resolved_node = next(
                (
                    n
                    for n in resolved_nodes
                    if isinstance(n, dict) and n["id"] == node.id
                ),
                {},
            )
            candidates: list[str] = []
            if isinstance(node, SelectNode):
                ref = node.candidates.partition(".")[0]
                candidates = models_by_node.get(ref, [])
            nodes.append(
                GraphNode(
                    id=node.id,
                    type=node.type,
                    agents=refs,
                    needs=needs,
                    fan_out=_node_fan_out(node),
                    models=models_by_node.get(node.id, []),
                    blocks=_node_blocks(node, resolved_node),
                    candidate_models=candidates,
                    facts=_node_facts(node, resolved_node),
                    checkpoint=node.id in checkpoints,
                )
            )
            for source, port in _node_sources(node):
                edges.append(GraphEdge(source=source, target=node.id, port=port))
        return PipelineGraph(
            name=name,
            input_mode=graph.pipeline.input_mode,
            checkpoints=list(graph.pipeline.checkpoints),
            order=graph.order,
            nodes=nodes,
            edges=edges,
        )

    @api.post("/api/projects/{project_id}/pipelines/{name}/validate")
    def validate_pipeline(project_id: str, name: str) -> ValidateResponse:
        pdir = _project_dir(project_id)
        try:
            errors = _collect_errors(pdir, name, {})
        except UsageError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return ValidateResponse(ok=not errors, errors=errors)

    # --- runs ----------------------------------------------------------------

    @api.post("/api/projects/{project_id}/runs", status_code=202)
    async def start_run(project_id: str, req: StartRunRequest) -> StartRunResponse:
        pdir = _project_dir(project_id)
        overrides = req.overrides or {}
        # Pre-flight synchronously so we can return proper HTTP codes; run_impl
        # repeats these checks in the background thread (idempotent).
        try:
            errors = _collect_errors(pdir, req.pipeline, overrides)
        except UsageError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"errors": [e.model_dump() for e in errors]},
            )
        active = _active_run(pdir / "runs")
        if active is not None:
            raise HTTPException(
                status_code=409, detail=f"run {active} is already active"
            )

        run_id = _unique_run_id(pdir / "runs", st.runs)
        run_dir = pdir / "runs" / run_id
        rec = RunRecord(run_dir=run_dir, project_id=project_id)
        st.runs[run_id] = rec

        async def _launch() -> None:
            try:
                status, _ = await asyncio.to_thread(
                    run_impl,
                    pdir,
                    pipeline=req.pipeline,
                    app=st.app_config,
                    model_overrides=overrides,
                    runtime_factory=st.runtime_factory,
                    run_id=run_id,
                    force_nodes=req.force,
                    stop_after=req.stop_after,
                    reuse_run_id=req.reuse_from,
                    clock=st.clock,
                )
                rec.status = status.value
            except asyncio.CancelledError:
                rec.status = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001 — surface as run status
                rec.status = "failed"
                rec.error = str(e)

        rec.task = asyncio.create_task(_launch())
        return StartRunResponse(run_id=run_id)

    @api.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> RunState:
        run_dir = _find_run_dir(run_id)
        return RunState.model_validate(read_state(run_dir))

    @api.get("/api/runs/{run_id}/steps/{step_id}/artifacts")
    def list_artifacts(run_id: str, step_id: str) -> list[str]:
        run_dir = _find_run_dir(run_id)
        base = _artifact_base(run_dir, step_id)
        return sorted(
            p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()
        )

    @api.get("/api/runs/{run_id}/steps/{step_id}/artifacts/{path:path}")
    def get_artifact(run_id: str, step_id: str, path: str) -> FileResponse:
        run_dir = _find_run_dir(run_id)
        base = _artifact_base(run_dir, step_id).resolve()
        target = (base / path).resolve()
        if base not in target.parents and target != base:
            raise HTTPException(status_code=400, detail="path traversal rejected")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no artifact {path!r}")
        return FileResponse(target)

    @api.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, str]:
        rec = st.runs.get(run_id)
        if rec is None:
            # ensure the run at least exists on disk
            _find_run_dir(run_id)
            return {"status": "cancelled"}
        if rec.task is not None and not rec.task.done():
            rec.task.cancel()
        rec.status = "cancelled"
        return {"status": "cancelled"}

    @api.post("/api/runs/{run_id}/pause")
    def pause_run(run_id: str) -> None:
        raise HTTPException(status_code=501, detail="pause not implemented (phase 3)")

    @api.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, str]:
        run_dir = _find_run_dir(run_id)
        _guard_not_executing(run_id)
        rec = st.runs.get(run_id) or RunRecord(
            run_dir=run_dir, project_id=run_dir.parent.parent.name
        )
        st.runs[run_id] = rec

        rec.status = "running"
        rec.task = asyncio.create_task(_resume_task(rec, run_dir))
        return {"run_id": run_id}

    async def _resume_task(rec: RunRecord, run_dir: Path) -> None:
        """Resume a run, waiting briefly for its previous executor to let go.

        A parked run publishes ``waiting_human`` in the ledger a moment before its
        executor exits and drops ``.active.lock``. A client that answers instantly
        (the UI does) would otherwise hit that lock and the resume would be dropped
        on the floor — which is exactly what a live browser run showed.
        """
        for attempt in range(20):
            try:
                status = await asyncio.to_thread(
                    resume_impl,
                    run_dir,
                    app=st.app_config,
                    runtime_factory=st.runtime_factory,
                    clock=st.clock,
                )
                rec.status = status.value
                return
            except ActiveRunConflict:
                if attempt == 19:
                    rec.status = "failed"
                    rec.error = "run is still locked by another executor"
                    _log.warning("resume of %s gave up: still locked", run_dir.name)
                    return
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                rec.status = "cancelled"
                _log.warning("resume of %s was cancelled", run_dir.name)
                raise
            except Exception as e:  # noqa: BLE001 — surfaced as run status
                rec.status = "failed"
                rec.error = str(e)
                # without this the only trace of a failed resume was an in-memory
                # field no client ever reads
                _log.exception("resume of %s failed", run_dir.name)
                return

    def _guard_not_executing(run_id: str) -> None:
        """Refuse to start a second execution of a run already in flight (§16.1)."""
        rec = st.runs.get(run_id)
        if rec is not None and rec.task is not None and not rec.task.done():
            raise HTTPException(
                status_code=409, detail=f"run {run_id} is already executing"
            )

    @api.post("/api/runs/{run_id}/answers")
    async def answer_run(run_id: str, req: AnswerRequest) -> dict[str, str]:
        run_dir = _find_run_dir(run_id)
        _guard_not_executing(run_id)
        try:
            write_answer(run_dir, req.step_id, req.answer)
        except UsageError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        rec = st.runs.get(run_id) or RunRecord(
            run_dir=run_dir, project_id=run_dir.parent.parent.name
        )
        st.runs[run_id] = rec

        rec.status = "running"
        rec.task = asyncio.create_task(_resume_task(rec, run_dir))
        return {"run_id": run_id}

    # --- models + fs ---------------------------------------------------------

    @api.get("/api/models")
    def list_models() -> list[ProviderInfo]:
        available = st.app_config.available_providers
        out: list[ProviderInfo] = []
        for name, p in sorted(st.app_config.providers.providers.items()):
            out.append(
                ProviderInfo(
                    name=name,
                    api_key_env=p.api_key_env,
                    available=name in available,
                    max_concurrent=p.max_concurrent,
                    models=list(p.models),
                )
            )
        return out

    @api.get("/api/fs/browse")
    def browse(path: str = Query("")) -> list[FsEntry]:
        root = st.projects_root.resolve()
        target = (root / path).resolve() if path else root
        if root not in target.parents and target != root:
            raise HTTPException(status_code=400, detail="outside sandbox")
        if not target.is_dir():
            raise HTTPException(status_code=404, detail=f"no directory {path!r}")
        return [
            FsEntry(name=child.name, is_dir=child.is_dir())
            for child in sorted(target.iterdir())
        ]

    # --- WS events -----------------------------------------------------------

    @api.websocket("/api/runs/{run_id}/events")
    async def stream_events(
        ws: WebSocket, run_id: str, from_seq: int = Query(0)
    ) -> None:
        await ws.accept()
        try:
            run_dir = _resolve_run_dir_ws(st, run_id)
        except FileNotFoundError:
            await ws.close(code=4404)
            return
        events_path = run_dir / EVENTS_FILENAME
        state_path = run_dir / "state.json"
        sent = max(from_seq - 1, 0)  # highest seq already delivered
        try:
            while True:
                sent = await _flush(events_path, sent, ws)
                if _terminal(state_path):
                    # final drain to catch any events appended after we checked
                    await _flush(events_path, sent, ws)
                    break
                await asyncio.sleep(0.5)
            await ws.close()
        except WebSocketDisconnect:
            return

    if static_dir is not None and static_dir.is_dir():
        # mounted last so every /api route wins; html=True serves index.html for
        # unknown paths, which the hash-routed SPA needs
        from fastapi.staticfiles import StaticFiles

        api.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return api


# --- module-level file helpers ------------------------------------------------


def _text_hash(text: str) -> str:
    """sha256 of pipeline text — the editor's optimistic-locking token (§19.2)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_or_empty(path: Path) -> str:
    return path.read_text("utf-8") if path.exists() else ""


def _atomic_write(path: Path, text: str) -> None:
    """Write via tmp + os.replace, like the ledger (I3 / §19.2).

    A crash mid-write must not leave a truncated pipeline.yaml behind.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- module-level WS helpers -------------------------------------------------


async def _flush(path: Path, sent: int, ws: WebSocket) -> int:
    """Send every event record with ``seq > sent``; return the new high-water seq."""
    if not path.exists():
        return sent
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        seq = int(record.get("seq", 0))
        if seq > sent:
            await ws.send_json(record)
            sent = seq
    return sent


def _terminal(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    try:
        status = json.loads(state_path.read_text("utf-8")).get("status")
    except (OSError, json.JSONDecodeError):
        return False
    return status in _TERMINAL


def _resolve_run_dir_ws(st: _State, run_id: str) -> Path:
    rec = st.runs.get(run_id)
    if rec is not None and rec.run_dir.exists():
        return rec.run_dir
    for pdir in st.projects_root.iterdir():
        candidate = pdir / "runs" / run_id
        if candidate.exists():
            return candidate
    raise FileNotFoundError(run_id)


def _unique_run_id(runs_dir: Path, registry: dict[str, RunRecord]) -> str:
    base = _new_run_id()
    run_id = base
    n = 2
    while run_id in registry or (runs_dir / run_id).exists():
        run_id = f"{base}_{n}"
        n += 1
    return run_id


# --- module-level graph helpers ------------------------------------------------


def _node_agent_refs(node: Node) -> list[str]:
    """Agent refs a node binds, in the order the engine would run them."""
    if isinstance(node, AgentNode | DiscoverNode):
        return [node.agent]
    if isinstance(node, LoopNode):
        return [*(b.agent for b in node.body_chain), node.critic.agent]
    if isinstance(node, SelectNode):
        return [node.selector.agent]
    return []


def _node_models(resolved_node: dict[str, Any]) -> list[str]:
    """Effective model(s) of a resolved node — several only for ``map_over``."""
    over = resolved_node.get("map_over")
    if isinstance(over, dict) and isinstance(over.get("models"), list):
        return [str(m) for m in over["models"]]
    out: list[str] = []
    holders: list[Any] = []
    for name in ("params", "body", "critic", "selector"):
        holder = resolved_node.get(name)
        # a loop body may be a chain: every element carries its own model
        holders.extend(holder if isinstance(holder, list) else [holder])
    for holder in holders:
        if isinstance(holder, dict):
            model = holder.get("model")
            if isinstance(model, str) and model and model not in out:
                out.append(model)
    return out


def _node_blocks(node: Node, resolved: dict[str, Any]) -> list[GraphBlock]:
    """The agents a meta-node runs inside itself, with their effective models."""
    roles: list[tuple[str, str, Any]] = []
    if isinstance(node, LoopNode):
        body_raw = resolved.get("body")
        for i, element in enumerate(node.body_chain):
            raw = body_raw[i] if isinstance(body_raw, list) else body_raw
            roles.append((node.body_block_name(i), element.agent, raw))
        roles.append(("critic", node.critic.agent, resolved.get("critic")))
    elif isinstance(node, SelectNode):
        roles = [("selector", node.selector.agent, resolved.get("selector"))]
    out: list[GraphBlock] = []
    for role, agent, block in roles:
        model = block.get("model") if isinstance(block, dict) else None
        out.append(
            GraphBlock(
                role=role, agent=agent, model=model if isinstance(model, str) else None
            )
        )
    return out


def _node_facts(node: Node, resolved: dict[str, Any]) -> dict[str, str]:
    """Params worth showing on the node itself (SPEC §8.2), as short strings."""
    params = resolved.get("params")
    p = params if isinstance(params, dict) else {}
    facts: dict[str, str] = {}
    if isinstance(node, LoopNode):
        facts["rounds"] = f"≤{p.get('max_rounds', 3)}"
        facts["on max"] = str(p.get("on_max_rounds", "pass"))
    elif isinstance(node, SelectNode):
        facts["fallback"] = str(p.get("fallback", "first_ok"))
    elif isinstance(node, DiscoverNode):
        facts["min sources"] = str(p.get("min_sources", 1))
    elif isinstance(node, AgentNode) and (node.map or node.map_over):
        facts["workers"] = str(p.get("workers", 3))
        facts["min ok"] = str(p.get("min_ok", 1))
    return facts


def _node_fan_out(node: Node) -> str | None:
    if isinstance(node, AgentNode):
        if node.map is not None:
            return "map"
        if node.map_over is not None:
            return "map_over"
    return None


def _node_sources(node: Node) -> list[tuple[str, str]]:
    """``(source_node, port)`` pairs feeding a node — its incoming edges."""
    refs: list[str] = []
    if isinstance(node, AgentNode):
        refs = [*node.inputs.values()]
        if node.map is not None:
            refs.append(node.map)
    elif isinstance(node, DiscoverNode):
        refs = [*node.inputs.values()]
    elif isinstance(node, LoopNode):
        inputs: list[dict[str, str]] = [b.inputs for b in node.body_chain]
        inputs.append(node.critic.inputs)
        refs = [r for group in inputs for r in group.values() if not r.startswith("@")]
    elif isinstance(node, SelectNode):
        refs = [node.candidates]
    out: list[tuple[str, str]] = []
    for ref_s in refs:
        source, _, port = ref_s.partition(".")
        if source and port and (source, port) not in out:
            out.append((source, port))
    return out
