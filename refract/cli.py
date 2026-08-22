"""Typer CLI (SPEC §14).

Commands: ``validate``, ``run``, ``status``, ``resume`` (Phase 0) plus
``agents list``. ``rerun`` is Phase 1. The synchronous CLI calls
``asyncio.run(...)`` at the boundary.

The command bodies are thin wrappers over pure ``*_impl`` functions that take an
explicit :class:`AppConfig` and a ``runtime_factory``. Tests drive those
directly with :class:`MockRuntime` and a fixed ``run_id``/clock — no network, no
``~/.refract``, no real CLI (I7 / SPEC §18 ``test_cli``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import typer
import yaml

from refract.events import EventWriter, utcnow_iso
from refract.deliver import deliver
from refract.explain import as_dict as explain_as_dict
from refract.explain import diagnose
from refract.explain import render as render_explain
from refract.graph import (
    ValidationContext,
    load_agents,
    load_pipeline,
    parse_pipeline_file,
)
from refract.models.agent import AgentSpec, tier_at_least
from refract.models.config import McpFile, ProjectConfig, ProvidersFile
from refract.models.ledger import (
    Event,
    EventType,
    NodeStatus,
    RunState,
    RunStatus,
    StepState,
    StepStatus,
)
from refract.models.pipeline import AgentNode, Pipeline
from refract.registry import ArtifactRegistry
from refract.runtime.base import AgentRuntime
from refract.scheduler import node_dependencies, run_pipeline
from refract.reuse import changed_agent_refs, read_agents_lock
from refract.snapshot import build_snapshot, node_agent_refs
from refract.state import Ledger, read_state
from refract.templates_lib import find_template, list_templates

if TYPE_CHECKING:  # pragma: no cover - typing only (fastapi is an extra)
    from fastapi import FastAPI

# Exit codes (SPEC §14; test_cli asserts these).
EXIT_OK = 0
EXIT_RUN_FAILED = 1  # pipeline ran to a terminal `failed`
EXIT_VALIDATION = 2  # blocking validation errors
EXIT_CONFLICT = 3  # another active run in the project (.active.lock)
EXIT_USAGE = 4  # bad invocation / missing files

RuntimeFactory = Callable[["AppConfig", Pipeline], AgentRuntime]


# --- application configuration (~/.refract) ---------------------------------


@dataclass
class AppConfig:
    """Resolved application config: providers + library location (SPEC §7)."""

    library_path: Path
    providers: ProvidersFile = field(default_factory=ProvidersFile)
    mcp: McpFile = field(default_factory=McpFile)

    @property
    def known_providers(self) -> set[str]:
        return set(self.providers.providers)

    @property
    def available_providers(self) -> set[str]:
        """Which providers can actually run (SPEC §7, CHANGED in this fork).

        A provider that declares ``api_key_env`` is available when that variable is
        non-empty. A provider WITHOUT one is served by the Claude Code CLI, which
        authenticates from its own subscription login — for those, availability means
        the CLI is installed.
        """
        from refract.runtime.claude_code import cli_available

        available: set[str] = set()
        for name, p in self.providers.providers.items():
            if p.api_key_env is None:
                if cli_available():
                    available.add(name)
            elif os.environ.get(p.api_key_env, "").strip():
                available.add(name)
        return available

    @property
    def provider_limits(self) -> dict[str, int]:
        return {n: p.max_concurrent for n, p in self.providers.providers.items()}


def refract_home() -> Path:
    """``$REFRACT_HOME`` or ``~/.refract`` (overridable for tests)."""
    override = os.environ.get("REFRACT_HOME")
    return Path(override) if override else Path.home() / ".refract"


def load_app_config() -> AppConfig:
    """Load ``providers.yaml`` and resolve the library path (SPEC §7).

    ``library_path`` comes from ``$REFRACT_LIBRARY`` first, then
    ``providers.yaml``; missing → :class:`UsageError`.
    """
    home = refract_home()
    providers = ProvidersFile()
    providers_file = home / "providers.yaml"
    if providers_file.exists():
        raw = yaml.safe_load(providers_file.read_text("utf-8")) or {}
        providers = ProvidersFile.model_validate(raw)

    mcp = McpFile()
    mcp_file = home / "mcp.yaml"
    if mcp_file.exists():
        mcp = McpFile.model_validate(yaml.safe_load(mcp_file.read_text("utf-8")) or {})

    lib = os.environ.get("REFRACT_LIBRARY") or providers.library_path
    if not lib:
        raise UsageError(
            "no library_path configured "
            f"(set $REFRACT_LIBRARY or library_path in {providers_file})"
        )
    return AppConfig(library_path=Path(lib), providers=providers, mcp=mcp)


class UsageError(Exception):
    """A user-facing invocation error → exit code ``EXIT_USAGE``."""


# --- live progress (SPEC §14: heartbeat lines in stdout) ---------------------


def progress_line(record: Event) -> str | None:
    """Render one event as a progress line, or ``None`` to stay silent.

    ASCII only: a run's stdout is read on Windows consoles whose code page is
    not UTF-8.
    """
    payload = record.payload
    step = record.step_id or ""
    if record.type is EventType.heartbeat:
        return f"  .. {step} {payload.get('elapsed_s')}s"
    if record.type is EventType.step_state_changed:
        to = str(payload.get("to", ""))
        if to == StepStatus.running.value:
            return f"  -> {step}"
        outcome = payload.get("outcome")
        if to == StepStatus.done.value:
            return f"  ok {step}"
        if to == StepStatus.reused.value:  # not a failure — don't shout it
            return f"  reused {step}"
        if to == StepStatus.waiting_human.value:
            return f"  ?? {step} waiting for human (refract answer)"
        detail = f" ({outcome})" if outcome else ""
        return f"  {to.upper()} {step}{detail}"
    if record.type is EventType.node_state_changed:
        node = payload.get("node_id", step)
        to = str(payload.get("to", ""))
        if to in {NodeStatus.done.value, NodeStatus.reused.value}:
            return f"node {node}: {to}"
        if to in {NodeStatus.failed.value, NodeStatus.skipped.value}:
            return f"node {node}: {to.upper()}"
        return None
    if record.type is EventType.question:
        return f"  ?? {step}: {payload.get('question')}"
    return None


class ProgressEventWriter(EventWriter):
    """``EventWriter`` that also streams progress to stdout (SPEC §14).

    Still the single writer of ``events.jsonl`` (§9) — rendering is a side
    effect of the same ``emit``, so stdout can never disagree with the file.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        clock: Callable[[], str] = utcnow_iso,
        echo: Callable[[str], None] = typer.echo,
    ) -> None:
        super().__init__(run_dir, clock=clock)
        self._echo = echo

    def emit(self, event: Mapping[str, object]) -> Event:
        record = super().emit(event)
        line = progress_line(record)
        if line is not None:
            self._echo(line)
        return record


# --- project + pipeline resolution ------------------------------------------


@dataclass
class LoadedProject:
    project_dir: Path
    config: ProjectConfig
    pipeline_name: str
    pipeline_path: Path

    @property
    def input_dir(self) -> Path:
        return (self.project_dir / self.config.input).resolve()

    @property
    def runs_dir(self) -> Path:
        return self.project_dir / "runs"


def resolve_project(
    project_dir: Path | str,
    pipeline: str | None,
    *,
    library_path: Path | None = None,
    home: Path | None = None,
) -> LoadedProject:
    """Load ``project.yaml`` and select the pipeline file (SPEC §7/§14).

    Two ways for a project to have a pipeline, and they are alternatives rather than a
    precedence order. ``pipeline: <name>`` in ``project.yaml`` REFERENCES a library
    template — right when the project differs from its siblings only by subject, which
    for a document conveyor is the usual case: the topic lives in the brief, and ten
    articles otherwise carry ten byte-identical copies that a template fix never
    reaches. Files in ``pipelines/`` are a project's OWN pipeline — right when it states
    its own terms and expects to diverge.

    Setting both is refused. The run is pinned by the snapshot either way, so nothing
    downstream can tell which was meant, and guessing is cheap to get wrong and
    invisible when it is.

    ``--pipeline`` is required when ``pipelines/`` holds more than one file.
    """
    project_dir = Path(project_dir)
    project_file = project_dir / "project.yaml"
    if not project_file.exists():
        raise UsageError(f"no project.yaml in {project_dir}")
    config = ProjectConfig.model_validate(
        yaml.safe_load(project_file.read_text("utf-8")) or {}
    )
    pipelines_dir = project_dir / "pipelines"
    available = (
        sorted(p for p in pipelines_dir.glob("*.yaml"))
        if pipelines_dir.is_dir()
        else []
    )
    if config.pipeline is not None:
        if available:
            names = ", ".join(p.stem for p in available)
            raise UsageError(
                f"project.yaml names the template {config.pipeline!r} and "
                f"{pipelines_dir} also holds {names}: say which one is meant — "
                "reference a template OR keep a local pipeline, not both"
            )
        if library_path is None:
            raise UsageError(
                f"project.yaml names the template {config.pipeline!r}, but no library "
                "was configured to resolve it from"
            )
        ref = find_template(config.pipeline, library_path, home or refract_home())
        if ref is None:
            known = ", ".join(
                t.name for t in list_templates(library_path, home or refract_home())
            )
            raise UsageError(
                f"no template {config.pipeline!r} in the library (have: {known})"
            )
        if pipeline is not None and pipeline != config.pipeline:
            raise UsageError(
                f"--pipeline {pipeline!r} contradicts project.yaml, which references "
                f"the template {config.pipeline!r}"
            )
        return LoadedProject(
            project_dir=project_dir,
            config=config,
            pipeline_name=ref.name,
            pipeline_path=ref.path,
        )
    if not available:
        raise UsageError(
            f"no pipelines in {pipelines_dir} and project.yaml names no template "
            "(add `pipeline: <name>` — see `refract templates`)"
        )
    if pipeline is not None:
        path = pipelines_dir / f"{pipeline}.yaml"
        if not path.exists():
            raise UsageError(f"pipeline {pipeline!r} not found: {path}")
    elif len(available) == 1:
        path = available[0]
    else:
        names = ", ".join(p.stem for p in available)
        raise UsageError(f"--pipeline is required (choices: {names})")
    return LoadedProject(
        project_dir=project_dir,
        config=config,
        pipeline_name=path.stem,
        pipeline_path=path,
    )


def _load_snapshot(
    run_dir: Path, *, library_path: Path
) -> tuple[Pipeline, dict[str, AgentSpec]]:
    """Load the effective (resolved) pipeline + agents from a run's snapshot (§9).

    Execution and resume read ONLY the snapshot — ``resolved.yaml`` carries the
    effective ``model``/params, and ``snapshot/agents/<ref>/`` the locked packages.
    """
    snap = run_dir / "snapshot"
    pipeline = Pipeline.model_validate(
        yaml.safe_load((snap / "resolved.yaml").read_text("utf-8")) or {}
    )
    agents, _ = load_agents(snap)
    return pipeline, agents


def _confirm_caps(config: ProjectConfig, agents: dict[str, AgentSpec]) -> set[str]:
    """Capabilities requiring human confirmation: explicit ``confirm`` + everything
    at/above ``confirm_tier`` that some used agent needs (SPEC §17 phase 3)."""
    caps = set(config.confirm)
    if config.confirm_tier:
        for agent in agents.values():
            caps.update(c for c in agent.needs if tier_at_least(c, config.confirm_tier))
    return caps


def _build_context(
    app: AppConfig,
    *,
    registry: ArtifactRegistry,
    agents: dict[str, AgentSpec],
    default_model: str | None,
    model_overrides: dict[str, str],
) -> ValidationContext:
    return ValidationContext(
        registry=registry,
        agents=agents,
        known_providers=app.known_providers,
        available_providers=app.available_providers,
        known_mcp_servers=set(app.mcp.servers),
        default_model=default_model,
        model_overrides=model_overrides,
    )


# --- .active.lock (one active run per project, SPEC §9/§16) ------------------

_LOCK_NAME = ".active.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check (Windows + POSIX)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _active_run(runs_dir: Path) -> str | None:
    """Return the run_id of a live active run in the project, if any (§16.1)."""
    if not runs_dir.is_dir():
        return None
    for run_dir in sorted(runs_dir.iterdir()):
        lock = run_dir / _LOCK_NAME
        if not lock.exists():
            continue
        try:
            pid = int(lock.read_text("utf-8").strip() or "0")
        except ValueError:
            pid = 0
        if _pid_alive(pid):
            return run_dir.name
        lock.unlink(missing_ok=True)  # stale lock → reclaim
    return None


def _write_lock(run_dir: Path) -> None:
    (run_dir / _LOCK_NAME).write_text(str(os.getpid()), encoding="utf-8")


def _clear_lock(run_dir: Path) -> None:
    (run_dir / _LOCK_NAME).unlink(missing_ok=True)


# --- shared helpers ----------------------------------------------------------


def _parse_kv(items: Iterable[str], *, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise UsageError(f"{flag} expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _apply_workers(pipeline: Pipeline, workers_for: dict[str, int]) -> None:
    """Apply ``--workers-for NODE=N`` onto the (already-validated) pipeline (§14)."""
    by_id = {n.id: n for n in pipeline.nodes}
    for nid, n in workers_for.items():
        node = by_id.get(nid)
        if node is None or not isinstance(node, AgentNode):
            raise UsageError(f"--workers-for: no agent node {nid!r}")
        node.params.workers = n


def _new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return "run_" + now.strftime("%Y%m%d_%H%M%S")


def _default_runtime_factory(app: AppConfig, pipeline: Pipeline) -> AgentRuntime:
    """Build the Claude Code runtime from app config (SPEC §12).

    Execution is not covered by the automated suite (it needs the real CLI and a live
    subscription); tests inject a MockRuntime via the ``runtime_factory`` parameter
    instead, and the adapter's pure parts have their own tests.
    """
    from refract.runtime.claude_code import ClaudeCodeRuntime

    # Which provider key variables a step may see (I8): only those declared by providers
    # this app knows, and only their NAMES travel — values stay in the environment.
    key_vars = [
        p.api_key_env
        for p in app.providers.providers.values()
        if p.api_key_env is not None
    ]
    return ClaudeCodeRuntime(mcp=app.mcp, provider_key_vars=key_vars)


def _print_errors(errors: Sequence[object]) -> None:
    for e in errors:
        code = getattr(e, "code", None)
        node = getattr(e, "node_id", None)
        msg = getattr(e, "message", str(e))
        code_s = getattr(code, "value", str(code))
        where = f" [{node}]" if node else ""
        typer.echo(f"{code_s}{where}: {msg}", err=True)


# --- validate ----------------------------------------------------------------


def validate_impl(
    project_dir: Path | str,
    *,
    pipeline: str | None = None,
    app: AppConfig,
) -> int:
    """Load + validate a project's pipeline; return an exit code (SPEC §8/§14)."""
    proj = resolve_project(project_dir, pipeline, library_path=app.library_path)
    registry = ArtifactRegistry.load(app.library_path)
    agents, agent_errors = load_agents(app.library_path)
    ctx = _build_context(
        app,
        registry=registry,
        agents=agents,
        default_model=proj.config.defaults.model,
        model_overrides={},
    )
    graph = load_pipeline(proj.pipeline_path, ctx)
    errors = list(agent_errors) + list(graph.errors)
    warnings = [e for e in errors if getattr(e.code, "is_warning", False)]
    blocking = [e for e in errors if not getattr(e.code, "is_warning", False)]
    _print_errors(errors)
    if blocking:
        typer.echo(
            f"INVALID: {len(blocking)} error(s), {len(warnings)} warning(s)", err=True
        )
        return EXIT_VALIDATION
    typer.echo(f"OK: {proj.pipeline_name} ({len(warnings)} warning(s))")
    return EXIT_OK


# --- run ---------------------------------------------------------------------


def run_impl(
    project_dir: Path | str,
    *,
    pipeline: str | None = None,
    app: AppConfig,
    model_overrides: dict[str, str] | None = None,
    workers_for: dict[str, int] | None = None,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    run_id: str | None = None,
    force_nodes: list[str] | None = None,
    stop_after: list[str] | None = None,
    reuse_run_id: str | None = None,
    clock: Callable[[], str] = utcnow_iso,
) -> tuple[RunStatus, Path]:
    """Validate, snapshot and execute a pipeline; return ``(status, run_dir)``.

    Enforces one active run per project via ``.active.lock`` (§16.1). The runtime
    is built by ``runtime_factory`` so tests inject :class:`MockRuntime`. When
    ``reuse_run_id`` is given (via :func:`rerun_impl`) unchanged nodes are reused
    from that prior run and ``force_nodes`` seeds the recompute set (SPEC §10.5).
    """
    model_overrides = model_overrides or {}
    proj = resolve_project(project_dir, pipeline, library_path=app.library_path)
    registry = ArtifactRegistry.load(app.library_path)
    agents, agent_errors = load_agents(app.library_path)
    ctx = _build_context(
        app,
        registry=registry,
        agents=agents,
        default_model=proj.config.defaults.model,
        model_overrides=model_overrides,
    )
    graph = load_pipeline(proj.pipeline_path, ctx)
    blocking = [
        e
        for e in (list(agent_errors) + list(graph.errors))
        if not getattr(e.code, "is_warning", False)
    ]
    if graph.pipeline is None or blocking:
        _print_errors(list(agent_errors) + list(graph.errors))
        raise ValidationFailed()
    pipeline_obj = graph.pipeline
    if workers_for:
        _apply_workers(pipeline_obj, workers_for)

    for nid in force_nodes or []:
        if nid not in {n.id for n in pipeline_obj.nodes}:
            raise UsageError(f"--from: no node {nid!r} in pipeline")
    if stop_after:
        known = {n.id for n in pipeline_obj.nodes}
        for nid in stop_after:
            if nid not in known:
                raise UsageError(f"--stop-after: no node {nid!r} in pipeline")
        # recorded in the snapshot (SPEC §21.1) so a resume keeps the same
        # checkpoints as the original run
        pipeline_obj.checkpoints = sorted(
            set(pipeline_obj.checkpoints) | set(stop_after)
        )

    active = _active_run(proj.runs_dir)
    if active is not None:
        raise ActiveRunConflict(active)

    reuse_run_dir = proj.runs_dir / reuse_run_id if reuse_run_id else None
    if reuse_run_dir is not None and not (reuse_run_dir / "state.json").exists():
        raise UsageError(f"--reuse: run {reuse_run_id!r} not found in {proj.runs_dir}")

    run_id = run_id or _new_run_id()
    run_dir = proj.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot(
        run_dir,
        pipeline_path=proj.pipeline_path,
        pipeline=pipeline_obj,
        library_path=app.library_path,
        agents=agents,
        overrides=model_overrides,
        default_model=proj.config.defaults.model,
    )
    # An agent package that changed since the run being reused cannot have its node
    # reused: the output on disk was produced by a different prompt. The lock has
    # recorded these hashes from the beginning and nothing compared them, so the one
    # case a person actually hits — fix a prompt, rerun — silently kept the old result.
    if reuse_run_dir is not None:
        changed = changed_agent_refs(
            read_agents_lock(reuse_run_dir), snapshot.agents_lock
        )
        if changed:
            per_node = node_agent_refs(pipeline_obj)
            stale = sorted(nid for nid, refs in per_node.items() if refs & changed)
            force_nodes = sorted(set(force_nodes or []) | set(stale))
            # ASCII only, like every other line this command prints: a run's stdout is
            # read on Windows consoles whose code page is not UTF-8, and an arrow here
            # raised UnicodeEncodeError from cp1252 and killed the run before its first
            # step — the message about what would be recomputed became the reason nothing
            # was.
            typer.echo(
                f"agents changed since {reuse_run_id}: {', '.join(sorted(changed))}"
                f" -> recomputing {', '.join(stale)}"
            )
    # Execute from the snapshot (I7/§9): resolved.yaml carries effective models.
    exec_pipeline, exec_agents = _load_snapshot(run_dir, library_path=app.library_path)
    ledger = Ledger.create(
        run_dir,
        run_id=run_id,
        pipeline=proj.pipeline_name,
        node_ids=[n.id for n in exec_pipeline.nodes],
        created_at=clock(),
        reuse_from=reuse_run_id,
        force_nodes=force_nodes,
    )
    events = ProgressEventWriter(run_dir, clock=clock)
    runtime = runtime_factory(app, exec_pipeline)

    verb = f"rerun {run_id} (reuse {reuse_run_id})" if reuse_run_id else f"run {run_id}"
    typer.echo(f"{verb}: {proj.pipeline_name} ({len(exec_pipeline.nodes)} nodes)")
    _write_lock(run_dir)
    try:
        status = asyncio.run(
            run_pipeline(
                run_dir,
                pipeline=exec_pipeline,
                agents=exec_agents,
                registry=registry,
                runtime=runtime,
                ledger=ledger,
                events=events,
                provider_limits=app.provider_limits,
                project_input_dir=proj.input_dir,
                reuse_run_dir=reuse_run_dir,
                confirm_capabilities=_confirm_caps(proj.config, exec_agents),
                stop_after=stop_after,
                clock=clock,
            )
        )
    finally:
        asyncio.run(runtime.close())
        _clear_lock(run_dir)
    _print_run_summary(ledger)
    return status, run_dir


def _resolve_reuse_run(runs_dir: Path, reuse: str) -> str:
    """Resolve ``--reuse RUN|last`` to a concrete run id (SPEC §14)."""
    if reuse != "last":
        return reuse
    candidates = sorted(
        p.name
        for p in (runs_dir.iterdir() if runs_dir.is_dir() else [])
        if p.is_dir() and (p / "state.json").exists()
    )
    if not candidates:
        raise UsageError(f"--reuse last: no prior runs in {runs_dir}")
    return candidates[-1]


def rerun_impl(
    project_dir: Path | str,
    *,
    from_node: str,
    reuse: str = "last",
    pipeline: str | None = None,
    app: AppConfig,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    run_id: str | None = None,
    clock: Callable[[], str] = utcnow_iso,
) -> tuple[RunStatus, Path]:
    """Rerun-from-node: a new run reusing unchanged nodes from a prior run (§10.5/§14)."""
    proj = resolve_project(project_dir, pipeline, library_path=app.library_path)
    reuse_run_id = _resolve_reuse_run(proj.runs_dir, reuse)
    return run_impl(
        project_dir,
        pipeline=pipeline,
        app=app,
        runtime_factory=runtime_factory,
        run_id=run_id,
        force_nodes=[from_node],
        reuse_run_id=reuse_run_id,
        clock=clock,
    )


# --- resume ------------------------------------------------------------------


# Affirmative answers that approve a capability confirmation; anything else
# (incl. "no"/"reject") rejects it and fails the node.
_AFFIRMATIVE = frozenset(
    {
        "approve",
        "approved",
        "yes",
        "y",
        "ok",
        "okay",
        "continue",
        "go",
        "да",
        "ок",
        "дальше",
        "продолжай",
    }
)


def write_answer(run_dir: Path | str, step_id: str, answer: str) -> None:
    """Drop a human answer@v1 at the waiting step's ``hitl/answer.json`` (§16.9).

    The next resume folds it into that step's prompt so the agent proceeds.
    """
    run_dir = Path(run_dir)
    node_id, _, leaf = step_id.partition(":")
    checkpoint = run_dir / "steps" / node_id / "checkpoint" / "request.json"
    if checkpoint.exists():
        # a checkpoint parks the RUN after the node finished (SPEC §21), so there is
        # no waiting step to look up — the decision is the whole answer
        approved = answer.strip().casefold() in _AFFIRMATIVE
        (checkpoint.parent / "decision.json").write_text(
            json.dumps({"approved": approved, "answer": answer}, ensure_ascii=False),
            encoding="utf-8",
        )
        return
    step = ledger_step(run_dir, step_id)
    if step is None or step.status is not StepStatus.waiting_human:
        raise UsageError(f"step {step_id!r} is not waiting for a human answer")
    wd = run_dir / "steps" / node_id / (leaf or "main")
    request = wd / "confirm" / "request.json"
    if request.exists():  # a capability confirmation, not an agent question
        approved = answer.strip().casefold() in _AFFIRMATIVE
        (wd / "confirm" / "decision.json").write_text(
            json.dumps({"approved": approved, "answer": answer}, ensure_ascii=False),
            encoding="utf-8",
        )
        return
    hitl = wd / "hitl"
    hitl.mkdir(parents=True, exist_ok=True)
    (hitl / "answer.json").write_text(
        json.dumps({"answer": answer}, ensure_ascii=False), encoding="utf-8"
    )


def ledger_step(run_dir: Path, step_id: str) -> StepState | None:
    state = RunState.model_validate(read_state(run_dir))
    return state.steps.get(step_id)


def answer_impl(
    run_dir: Path | str,
    *,
    step_id: str,
    answer: str,
    app: AppConfig,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    clock: Callable[[], str] = utcnow_iso,
) -> RunStatus:
    """Supply a human answer to a waiting step, then resume the run (§16.9)."""
    write_answer(run_dir, step_id, answer)
    return resume_impl(run_dir, app=app, runtime_factory=runtime_factory, clock=clock)


def resume_impl(
    run_dir: Path | str,
    *,
    app: AppConfig,
    retry_failed: bool = False,
    force_step: str | None = None,
    runtime_factory: RuntimeFactory = _default_runtime_factory,
    clock: Callable[[], str] = utcnow_iso,
) -> RunStatus:
    """Resume a run from its snapshot; execution reads ONLY the snapshot (§9/§10.5)."""
    run_dir = Path(run_dir)
    if not (run_dir / "state.json").exists():
        raise UsageError(f"no state.json in {run_dir}")
    pipeline_obj, agents = _load_snapshot(run_dir, library_path=app.library_path)
    registry = ArtifactRegistry.load(app.library_path)

    # One active execution per project (§16.1) — including THIS run: a live lock on
    # the run being resumed means someone is already executing it, and two schedulers
    # over one ledger corrupt each other (seen live: a second resume killed the first
    # mid-node, leaving the run failed with a node stuck at `running`).
    active = _active_run(run_dir.parent)
    if active is not None:
        raise ActiveRunConflict(active)

    ledger = Ledger.load(run_dir)  # crash recovery: running → pending
    if force_step is not None:
        _force_step(ledger, run_dir, force_step, pipeline_obj)
    if retry_failed:
        _retry_failed(ledger)
    ledger.save()

    events = ProgressEventWriter(run_dir, clock=clock)
    runtime = runtime_factory(app, pipeline_obj)
    _write_lock(run_dir)
    try:
        status = asyncio.run(
            run_pipeline(
                run_dir,
                pipeline=pipeline_obj,
                agents=agents,
                registry=registry,
                runtime=runtime,
                ledger=ledger,
                events=events,
                provider_limits=app.provider_limits,
                project_input_dir=_recover_project_input(run_dir),
                confirm_capabilities=_recover_confirm_caps(run_dir, agents),
                clock=clock,
            )
        )
    finally:
        asyncio.run(runtime.close())
        _clear_lock(run_dir)
    _print_run_summary(ledger)
    return status


def _recover_project_input(run_dir: Path) -> Path | None:
    """Best-effort project input dir for resume: ``<project>/runs/<run>`` → project.

    Needed when a builtin that reads project input (e.g. scanner) must re-run on
    resume; the snapshot doesn't carry project input, but the run dir's location
    reveals the project root. ``None`` if the layout doesn't match.
    """
    project_dir = run_dir.parent.parent
    project_file = project_dir / "project.yaml"
    if run_dir.parent.name != "runs" or not project_file.exists():
        return None
    config = ProjectConfig.model_validate(
        yaml.safe_load(project_file.read_text("utf-8")) or {}
    )
    return (project_dir / config.input).resolve()


def _recover_confirm_caps(run_dir: Path, agents: dict[str, AgentSpec]) -> set[str]:
    """Recover the project's confirm policy for a resume (§17 phase 3).

    The policy lives in project.yaml (not the snapshot); the run-dir layout reveals
    the project root. Empty set if the layout doesn't match.
    """
    project_file = run_dir.parent.parent / "project.yaml"
    if run_dir.parent.name != "runs" or not project_file.exists():
        return set()
    config = ProjectConfig.model_validate(
        yaml.safe_load(project_file.read_text("utf-8")) or {}
    )
    return _confirm_caps(config, agents)


def _retry_failed(ledger: Ledger) -> None:
    """``--retry-failed``: failed steps → pending and failed nodes → pending (§10.5)."""
    ledger.reset_failed_steps()
    for nid in ledger.node_ids():
        node = ledger.get_node(nid)
        if node is not None and node.status in (NodeStatus.failed, NodeStatus.skipped):
            ledger.set_node_status(nid, NodeStatus.pending, error=None)


def _force_step(
    ledger: Ledger, run_dir: Path, step_id: str, pipeline: Pipeline
) -> None:
    """``--force-step STEP_ID``: archive the step, reset it + its node + all
    downstream nodes to pending so their outputs are rebuilt on resume (§10.5)."""
    step = ledger.get_step(step_id)
    if step is None:
        raise UsageError(f"--force-step: unknown step {step_id!r}")
    node_id, _, leaf = step_id.partition(":")
    step_dir = run_dir / "steps" / node_id / (leaf or "main")
    if step_dir.is_dir():
        _archive_step_dir(step_dir)
    ledger.set_step(step_id, node=step.node, status=StepStatus.pending)
    for nid in {step.node, *_descendants(pipeline, step.node)}:
        node = ledger.get_node(nid)
        if node is not None:
            ledger.set_node_status(nid, NodeStatus.pending, error=None)


def _descendants(pipeline: Pipeline, node_id: str) -> set[str]:
    """All nodes transitively downstream of ``node_id`` (edges via the graph deps)."""
    deps = node_dependencies(pipeline)  # child -> set(parents)
    children: dict[str, set[str]] = {n.id: set() for n in pipeline.nodes}
    for child, parents in deps.items():
        for parent in parents:
            children.setdefault(parent, set()).add(child)
    out: set[str] = set()
    stack = list(children.get(node_id, set()))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, set()))
    return out


def _archive_step_dir(step_dir: Path) -> None:
    """Move a step dir under ``attempts/<n>/`` without clobbering (mirrors §10.2)."""
    attempts = step_dir / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    n = 1
    while (attempts / str(n)).exists():
        n += 1
    dest = attempts / str(n)
    dest.mkdir()
    for child in sorted(step_dir.iterdir()):
        if child.name == "attempts":
            continue
        child.rename(dest / child.name)


# --- status ------------------------------------------------------------------


def _project_pipeline_outputs(run_dir: Path) -> dict[str, str]:
    """``outputs`` from the project's CURRENT pipeline, or empty if unavailable.

    ``runs/<id>`` sits two levels under the project, which is how the rest of the CLI
    recovers a project from a run directory too.
    """
    project_dir = Path(run_dir).resolve().parents[1]
    try:
        proj = resolve_project(project_dir, None, library_path=None)
    except Exception:
        try:
            app = load_app_config()
            proj = resolve_project(project_dir, None, library_path=app.library_path)
        except Exception:
            return {}
    pipeline, errors = parse_pipeline_file(proj.pipeline_path)
    if pipeline is None or errors:
        return {}
    return dict(pipeline.outputs)


def deliver_impl(run_dir: Path | str, *, app: AppConfig) -> int:
    """Assemble a run's declared deliverables into ``runs/<id>/output/`` (SPEC §22).

    A completed run does this itself; the command exists for the cases where that is not
    enough — a run finished before the pipeline declared its outputs, or a delivery a
    person wants rebuilt after inspecting the step tree. It reports a missing artifact
    rather than assembling a folder that looks complete without being it.
    """
    run_dir = Path(run_dir)
    pipeline, agents = _load_snapshot(run_dir, library_path=app.library_path)
    registry = ArtifactRegistry.load(app.library_path)
    if not pipeline.outputs:
        # The snapshot is the authority on what RAN, and `outputs` is not about what ran
        # — it is about how the result is presented. So a run finished before the pipeline
        # declared its outputs can still be assembled from the project's current
        # declaration, and the substitution is announced rather than done quietly.
        current = _project_pipeline_outputs(run_dir)
        if current:
            typer.echo(
                f"the snapshot of this run declares no outputs; using the project's "
                f"current declaration ({', '.join(sorted(current))})"
            )
            pipeline = pipeline.model_copy(update={"outputs": current})
        else:
            typer.echo(
                f"pipeline {pipeline.name!r} declares no outputs: nothing to deliver"
            )
            return EXIT_OK
    report = deliver(run_dir, pipeline=pipeline, registry=registry, agents=agents)
    for line in report.render():
        typer.echo(f"  {line}")
    typer.echo(f"delivered to {report.output_dir}")
    if report.result_dir is not None:
        # Named second and last, because it is the path a person will actually open.
        typer.echo(f"latest (mirror of this run): {report.result_dir}")
    return EXIT_OK if report.ok else EXIT_RUN_FAILED


def status_impl(run_dir: Path | str) -> int:
    typer.echo(render_status(run_dir))
    return EXIT_OK


def explain_impl(run_dir: Path | str, *, as_json: bool = False) -> int:
    """Post-mortem of a run (SPEC §14). Reads the ledger, events and gate reports."""
    try:
        diagnosis = diagnose(run_dir)
    except FileNotFoundError as exc:
        raise UsageError(str(exc)) from exc
    if as_json:
        typer.echo(json.dumps(explain_as_dict(diagnosis), ensure_ascii=False, indent=2))
    else:
        typer.echo(render_explain(diagnosis))
    return EXIT_OK


def render_status(run_dir: Path | str) -> str:
    """Render the run status table from ``state.json`` only (I7)."""
    run_dir = Path(run_dir)
    state_file = run_dir / "state.json"
    if not state_file.exists():
        raise UsageError(f"no state.json in {run_dir}")
    state = RunState.model_validate(json.loads(state_file.read_text("utf-8")))
    lines = [
        f"run:      {state.run_id}",
        f"pipeline: {state.pipeline}",
        f"status:   {state.status.value}",
    ]
    if state.awaiting_checkpoint:
        parked = state.awaiting_checkpoint
        lines.append(f"awaiting checkpoint: {parked}")
        request = run_dir / "steps" / parked / "checkpoint" / "request.json"
        if request.exists():
            try:
                outputs = json.loads(request.read_text("utf-8")).get("outputs", [])
            except json.JSONDecodeError:
                outputs = []
            for path in outputs:
                lines.append(f"  output: {path}")
        lines.append(f"  continue with: refract answer {run_dir} {parked} continue")
    lines.append("nodes:")
    for nid, node in state.nodes.items():
        err = f"  ({node.error})" if node.error else ""
        lines.append(f"  {nid:<20} {node.status.value}{err}")
    if state.steps:
        lines.append("steps:")
        for sid, step in state.steps.items():
            out = f" {step.outcome.value}" if step.outcome else ""
            lines.append(f"  {sid:<28} {step.status.value}{out} (tries={step.tries})")
    return "\n".join(lines)


def _print_run_summary(ledger: Ledger) -> None:
    for nid in ledger.node_ids():
        node = ledger.get_node(nid)
        if node is not None:
            typer.echo(f"  {nid:<20} {node.status.value}")


# --- project scaffolding (SPEC §14) -----------------------------------------


def _available_templates(app: AppConfig) -> list[str]:
    """Template names from both sources — shipped and the user's (SPEC-UI §5)."""
    return [t.name for t in list_templates(app.library_path, refract_home())]


def templates_impl(app: AppConfig) -> int:
    """List available pipeline templates (SPEC §14)."""
    refs = list_templates(app.library_path, refract_home())
    if not refs:
        typer.echo(f"no templates in {app.library_path / 'templates'}")
        return EXIT_OK
    for ref in refs:
        typer.echo(f"{ref.name:<20} {ref.source}")
    return EXIT_OK


def catalog_impl(app: AppConfig, *, as_json: bool = False) -> int:
    """Print the authoring catalog (SPEC §19.1).

    ``--json`` gives a builder (UI or LLM) the whole document; the default is a
    human summary — the full JSON is thousands of lines.
    """
    from refract.catalog import build_catalog

    catalog = build_catalog(app.library_path)
    if as_json:
        typer.echo(json.dumps(catalog, ensure_ascii=False, indent=2))
        return EXIT_OK

    typer.echo(f"catalog {catalog['version']} from {app.library_path}")
    typer.echo(f"\nagents ({len(catalog['agents'])}):")
    for entry in catalog["agents"]:
        consumes = ", ".join(f"{p['port']}:{p['type']}" for p in entry["consumes"])
        produces = ", ".join(f"{p['port']}:{p['type']}" for p in entry["produces"])
        typer.echo(f"  {entry['ref']:<28} {consumes or '-'} -> {produces}")
        typer.echo(f"  {'':<28} needs: {', '.join(entry['needs']) or '-'}")
    typer.echo(f"\nbuiltins ({len(catalog['builtins'])}):")
    for entry in catalog["builtins"]:
        produces = ", ".join(f"{p['port']}:{p['type']}" for p in entry["produces"])
        typer.echo(f"  {entry['type']:<28} -> {produces}")
    typer.echo(f"\nnode kinds: {', '.join(k['kind'] for k in catalog['node_kinds'])}")
    typer.echo(f"artifact types: {len(catalog['artifact_types'])}")
    typer.echo(f"templates: {', '.join(catalog['templates']) or '-'}")
    typer.echo(f"constraints: {len(catalog['constraints'])} (use --json for the rules)")
    return EXIT_OK


DEFAULT_WORKSPACE = "projects"


def workspace_dir(override: str | None = None) -> Path:
    """The workspace holding one directory per project (SPEC-UI §2).

    ``--projects-root`` wins, then ``$REFRACT_WORKSPACE``, else
    ``<refract_home>/projects``. Created on demand so a first run just works.
    """
    raw = override or os.environ.get("REFRACT_WORKSPACE")
    path = Path(raw) if raw else refract_home() / DEFAULT_WORKSPACE
    path.mkdir(parents=True, exist_ok=True)
    return path


def web_dist() -> Path | None:
    """The built SPA directory, if this checkout has one (``web/dist``)."""
    candidate = Path(__file__).resolve().parent.parent / "web" / "dist"
    return candidate if (candidate / "index.html").exists() else None


def build_api(app: AppConfig, *, projects_root: Path) -> "FastAPI":
    """Build the FastAPI app (SPEC §15) — separated so tests skip uvicorn."""
    from refract.api import create_app

    return create_app(
        projects_root=projects_root, app_config=app, static_dir=web_dist()
    )


def serve_impl(
    app: AppConfig,
    *,
    projects_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    runner: Callable[["FastAPI", str, int], None] | None = None,
) -> int:
    """Serve the REST/WS API (SPEC §15) over the workspace.

    Binds to localhost by default: the API runs pipelines and browses the
    filesystem, so it is a local tool, not something to expose (I8).
    """
    api = build_api(app, projects_root=projects_root)
    ui = web_dist()
    typer.echo(f"serving {projects_root} on http://{host}:{port}")
    typer.echo(
        f"  ui: {'bundled' if ui else 'not built (run `npm run build` in web/)'}"
    )
    if runner is None:  # pragma: no cover - real server loop
        import uvicorn

        uvicorn.run(api, host=host, port=port, log_level="info")
    else:
        runner(api, host, port)
    return EXIT_OK


def init_impl(
    project_dir: Path | str,
    *,
    template: str,
    app: AppConfig,
    name: str | None = None,
    model: str = "claude/sonnet",
    force: bool = False,
    input_dir: str | None = None,
) -> int:
    """Scaffold a new project from a template (SPEC §14, SPEC-UI §5).

    Copies the template (shipped or the user's own, same resolution the API uses)
    into ``<project>/pipelines/`` and writes a minimal ``project.yaml``. Without
    ``input_dir`` the project gets its own empty ``input/``; with it the documents
    folder is referenced as given and nothing is copied. Pure scaffolding — the
    caller runs ``refract validate`` next. Refuses to clobber an existing
    ``project.yaml`` unless ``force``.
    """
    project_dir = Path(project_dir)
    ref = find_template(template, app.library_path, refract_home())
    if ref is None:
        choices = ", ".join(_available_templates(app)) or "(none)"
        raise UsageError(f"unknown template {template!r} (available: {choices})")
    src = ref.path
    project_file = project_dir / "project.yaml"
    if project_file.exists() and not force:
        raise UsageError(f"{project_file} already exists (use --force to overwrite)")

    (project_dir / "pipelines").mkdir(parents=True, exist_ok=True)
    (project_dir / "pipelines" / f"{template}.yaml").write_text(
        src.read_text("utf-8"), encoding="utf-8"
    )
    if input_dir is None:
        (project_dir / "input").mkdir(parents=True, exist_ok=True)
    config = {
        "version": "0.1",
        "name": name or project_dir.resolve().name,
        "input": input_dir or "./input",
        "defaults": {"model": model},
    }
    project_file.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    typer.echo(f"initialized {project_dir} from template {template!r} ({ref.source})")
    where = input_dir or "./input"
    typer.echo(
        f"next: put sources in {where}, then `refract validate` and `refract run`"
    )
    return EXIT_OK


# --- exceptions mapped to exit codes ----------------------------------------


class ValidationFailed(Exception):
    pass


class ActiveRunConflict(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


# --- Typer wiring ------------------------------------------------------------

app = typer.Typer(
    add_completion=False, help="refract — declarative agent pipeline engine"
)
agents_app = typer.Typer(help="agent package commands")
app.add_typer(agents_app, name="agents")


def _run_cli(fn: Callable[[], int]) -> None:
    """Run a command body, mapping known errors to exit codes."""
    try:
        raise typer.Exit(code=fn())
    except UsageError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=EXIT_USAGE) from e
    except ValidationFailed as e:
        typer.echo("INVALID: pipeline has blocking errors", err=True)
        raise typer.Exit(code=EXIT_VALIDATION) from e
    except ActiveRunConflict as e:
        typer.echo(f"error: run {e.run_id} is already active in this project", err=True)
        raise typer.Exit(code=EXIT_CONFLICT) from e


@app.command()
def validate(
    project_dir: Path = typer.Argument(..., help="project directory"),
    pipeline: str | None = typer.Option(None, "--pipeline", help="pipeline name"),
) -> None:
    """Validate a project's pipeline (exit 0 ok, 2 invalid)."""
    _run_cli(
        lambda: validate_impl(project_dir, pipeline=pipeline, app=load_app_config())
    )


@app.command()
def run(
    project_dir: Path = typer.Argument(..., help="project directory"),
    pipeline: str | None = typer.Option(None, "--pipeline"),
    model_for: list[str] = typer.Option([], "--model-for", help="KEY=MODEL"),
    workers_for: list[str] = typer.Option([], "--workers-for", help="NODE=N"),
    stop_after: list[str] = typer.Option(
        [], "--stop-after", help="park the run after NODE for review (§21)"
    ),
) -> None:
    """Run a pipeline end-to-end."""

    def body() -> int:
        overrides = _parse_kv(model_for, flag="--model-for")
        workers = {
            k: int(v) for k, v in _parse_kv(workers_for, flag="--workers-for").items()
        }
        status, run_dir = run_impl(
            project_dir,
            pipeline=pipeline,
            app=load_app_config(),
            model_overrides=overrides,
            workers_for=workers,
            stop_after=list(stop_after),
        )
        if status is RunStatus.waiting_human:
            typer.echo(render_status(run_dir))
            return EXIT_OK
        return EXIT_OK if status is RunStatus.completed else EXIT_RUN_FAILED

    _run_cli(body)


@app.command()
def rerun(
    project_dir: Path = typer.Argument(..., help="project directory"),
    from_node: str = typer.Option(..., "--from", help="node id to recompute from"),
    reuse: str = typer.Option("last", "--reuse", help="RUN id or 'last'"),
    pipeline: str | None = typer.Option(None, "--pipeline"),
) -> None:
    """Rerun from a node, reusing unchanged upstream nodes from a prior run."""

    def body() -> int:
        status_, _ = rerun_impl(
            project_dir,
            from_node=from_node,
            reuse=reuse,
            pipeline=pipeline,
            app=load_app_config(),
        )
        return EXIT_OK if status_ is RunStatus.completed else EXIT_RUN_FAILED

    _run_cli(body)


@app.command(name="deliver")
def deliver_cmd(
    run_dir: Path = typer.Argument(..., help="run directory"),
) -> None:
    """Assemble the run's declared outputs into runs/<id>/output/."""
    _run_cli(lambda: deliver_impl(run_dir, app=load_app_config()))


@app.command()
def status(run_dir: Path = typer.Argument(..., help="run directory")) -> None:
    """Print a run's status from state.json."""
    _run_cli(lambda: status_impl(run_dir))


@app.command()
def explain(
    run_dir: Path = typer.Argument(..., help="run directory"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """Explain a run: what it cost, what broke first, and what barely passed."""
    _run_cli(lambda: explain_impl(run_dir, as_json=as_json))


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="run directory"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    force_step: str | None = typer.Option(None, "--force-step", help="STEP_ID"),
) -> None:
    """Resume a run from its snapshot."""

    def body() -> int:
        status_ = resume_impl(
            run_dir,
            app=load_app_config(),
            retry_failed=retry_failed,
            force_step=force_step,
        )
        return EXIT_OK if status_ is RunStatus.completed else EXIT_RUN_FAILED

    _run_cli(body)


@app.command()
def answer(
    run_dir: Path = typer.Argument(..., help="run directory"),
    step_id: str = typer.Argument(..., help="the waiting step id"),
    text: str = typer.Argument(..., help="the human answer"),
) -> None:
    """Answer a step that is waiting for a human, then resume the run (§16.9)."""

    def body() -> int:
        status_ = answer_impl(
            run_dir, step_id=step_id, answer=text, app=load_app_config()
        )
        return EXIT_OK if status_ is RunStatus.completed else EXIT_RUN_FAILED

    _run_cli(body)


@app.command()
def init(
    project_dir: Path = typer.Argument(..., help="project directory to create"),
    template: str = typer.Option(
        ..., "--template", "-t", help="pipeline template name"
    ),
    name: str | None = typer.Option(None, "--name", help="project name"),
    model: str = typer.Option(
        "claude/sonnet", "--model", help="default model (provider/model-id)"
    ),
    force: bool = typer.Option(
        False, "--force", help="overwrite existing project.yaml"
    ),
    input_dir: str | None = typer.Option(
        None, "--input", help="documents folder to read (any path; not copied)"
    ),
) -> None:
    """Scaffold a new project from a template (§14)."""
    _run_cli(
        lambda: init_impl(
            project_dir,
            template=template,
            app=load_app_config(),
            name=name,
            model=model,
            force=force,
            input_dir=input_dir,
        )
    )


@app.command()
def templates() -> None:
    """List available pipeline templates from the library (§14)."""
    _run_cli(lambda: templates_impl(load_app_config()))


@app.command()
def serve(
    projects_root: Path | None = typer.Option(
        None, "--projects-root", help="workspace dir (default <refract_home>/projects)"
    ),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Serve the REST/WS API for the UI (§15)."""
    _run_cli(
        lambda: serve_impl(
            load_app_config(),
            projects_root=workspace_dir(str(projects_root) if projects_root else None),
            host=host,
            port=port,
        )
    )


@app.command()
def catalog(
    as_json: bool = typer.Option(False, "--json", help="full catalog as JSON"),
) -> None:
    """Print the catalog of blocks a pipeline can be built from (§19)."""
    _run_cli(lambda: catalog_impl(load_app_config(), as_json=as_json))


@agents_app.command("list")
def agents_list() -> None:
    """List agent packages found in the library."""

    def body() -> int:
        app_cfg = load_app_config()
        agents, errors = load_agents(app_cfg.library_path)
        _print_errors(list(errors))
        for ref in sorted(agents):
            spec = agents[ref]
            typer.echo(
                f"{ref:<28} {spec.description.strip().splitlines()[0] if spec.description else ''}"
            )
        return EXIT_OK

    _run_cli(body)


if __name__ == "__main__":
    app()
