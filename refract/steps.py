"""The ONE step lifecycle (SPEC §10.2).

Materialize inputs (I1) → assemble prompt (§11) → run the runtime with a
timeout (infra-error backoff retries, counter separate from the gate) → HITL
check (phases 0–2: a valid question artifact → failed_agent) → gate (existence
+ schema + rules); on gate failure, archive the attempt to ``attempts/<n>/``
and retry with gate_feedback up to ``gate_retries`` extra times → done/ok.

Meta-nodes (loop/select) and map REUSE this; never duplicate it. Timestamps and
sleep are injectable so tests stay deterministic and network-free.
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from refract.artifacts import (
    GatePort,
    GateReport,
    artifact_path,
    link_or_copy,
    long_path,
    materialize_collection,
    materialize_dir_or_any,
    materialize_file,
    materialize_map_item,
    run_gate,
    write_gate_report,
)
from refract.models.agent import AgentSpec
from refract.models.ledger import StepOutcome, StepState, StepStatus, Usage
from refract.models.types import ItemInfo, Rule
from refract.prompt import RevisionContext, build_task_prompt
from refract.registry import ArtifactRegistry, ResolvedType
from refract.runtime.base import AgentRuntime, EventCallback, StepResult, StepSpec
from refract.state import Ledger

_QUESTION_TYPE = "question@v1"
_ARCHIVED = ("prompt.md", "raw.txt", "agent.events.jsonl", "gate_report.json")


# --- input specs (what to materialize into the step workdir) ---------------


@dataclass(frozen=True)
class FileInput:
    port: str
    src: Path
    rtype: ResolvedType


@dataclass(frozen=True)
class DirAnyInput:
    port: str
    src: Path


@dataclass(frozen=True)
class CollectionInput:
    port: str
    src: Path


@dataclass(frozen=True)
class MapItemInput:
    port: str
    src: Path
    item: ItemInfo


@dataclass(frozen=True)
class AuxFileInput:
    """A file placed at ``input/<rel_path>`` verbatim (SPEC §10.3 loop revision).

    Used for the engine-injected ``_previous/<port>.<ext>`` and
    ``_verdict/verdict.json`` a loop body sees from round ≥ 2 (I1: relative only).
    """

    rel_path: str
    src: Path


InputSpec = FileInput | DirAnyInput | CollectionInput | MapItemInput | AuxFileInput


@dataclass
class AgentStepPlan:
    """Everything needed to run one agent step (SPEC §10.2)."""

    step_id: str
    node_id: str
    workdir: Path
    agent: AgentSpec
    agent_dir: Path
    model: str
    registry: ArtifactRegistry
    inputs: list[InputSpec] = field(default_factory=list)
    timeout_s: int = 3600
    gate_retries: int = 2
    infra_retries: int = 2
    revision: RevisionContext | None = None
    # Node-level tightening of the primary output's gate (SPEC §8 ``gate_rules``),
    # checked alongside the artifact type's own rules.
    gate_rules: list[Rule] = field(default_factory=list)
    # Extra semantic validation of ``output/`` beyond the schema gate; returns a
    # list of problems (empty = pass). Feeds the same gate-retry loop. Used by
    # select to require ``selection.winner`` ∈ ok-slugs (SPEC §10.3).
    extra_gate: Callable[[Path], list[str]] | None = None


# --- helpers ----------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_delay(infra_attempt: int) -> float:
    """base 2s, ×2, jitter, max 60s (SPEC §10.2 step 3)."""
    base = min(60.0, 2.0 * (2 ** (infra_attempt - 1)))
    return float(base * (0.5 + random.random() * 0.5))


def _gate_ports(
    agent: AgentSpec,
    registry: ArtifactRegistry,
    gate_rules: Sequence[Rule] = (),
) -> list[GatePort]:
    """Ports to validate; ``gate_rules`` tighten the PRIMARY one (SPEC §8/§10.2)."""
    ports: list[GatePort] = []
    for i, p in enumerate(agent.produces):
        rtype = registry.get(p.type)
        if rtype is not None:
            ports.append(
                GatePort(
                    port=p.port,
                    rtype=rtype,
                    optional=p.optional,
                    extra_rules=tuple(gate_rules) if i == 0 else (),
                    # where a ``forbid_file`` pattern list is read from
                    base_dir=registry.library_path,
                )
            )
    return ports


def _read_question(
    agent: AgentSpec, registry: ArtifactRegistry, output_dir: Path
) -> dict[str, object] | None:
    """The agent's valid ``question@v1`` HITL artifact, if it produced one (§10.2/§16.9).

    Returns the parsed question data (the control decision comes only from this
    typed artifact — I4), or None when there is no valid question on the single
    optional HITL port.
    """
    qtype = registry.get(_QUESTION_TYPE)
    if qtype is None:
        return None
    for p in agent.produces:
        if not (p.optional and p.type == _QUESTION_TYPE):
            continue
        path = artifact_path(output_dir, p.port, qtype)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if qtype.validate_json(data) == [] and isinstance(data, dict):
            return data
    return None


def _format_feedback(report: GateReport) -> str:
    lines = [
        f"- {pr.port}: {problem}"
        for pr in report.ports
        if not pr.ok
        for problem in pr.problems
    ]
    return "\n".join(lines)


def _materialize(inputs: list[InputSpec], input_root: Path) -> None:
    """Lay out ``input/<port>/`` for a step (SPEC §10.1).

    Rebuilt from scratch each time. Materialization is not additive: ``link_or_copy``
    requires a destination that does not exist yet, so a second pass over a workdir
    that already has ``input/`` raised ``FileExistsError`` — which meant every
    ``resume --retry-failed`` of a non-map step died on its own leftovers instead of
    retrying. Inputs are immutable, so rebuilding produces the same tree.
    """
    if input_root.exists():
        for entry in input_root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(long_path(entry), ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    for spec in inputs:
        if isinstance(spec, FileInput):
            materialize_file(spec.src, input_root, spec.port, spec.rtype)
        elif isinstance(spec, DirAnyInput):
            materialize_dir_or_any(spec.src, input_root, spec.port)
        elif isinstance(spec, CollectionInput):
            materialize_collection(spec.src, input_root, spec.port)
        elif isinstance(spec, MapItemInput):
            materialize_map_item(spec.src, input_root, spec.port, spec.item)
        elif isinstance(spec, AuxFileInput):
            dst = input_root / spec.rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            link_or_copy(spec.src, dst)


def _rejected_input(workdir: Path, archived: int) -> str | None:
    """COPY the archived attempt's ``output/`` in as ``input/_rejected/`` (SPEC §10.2).

    A gate retry is an EDIT, not a fresh start: the feedback names what is wrong with a
    specific document, and the document is the cheapest thing the step already has.

    A copy, not ``link_or_copy``. That helper prefers a symlink, and a symlink here points
    into ``attempts/<n>/output/`` — inside the workdir, so the guard permits writing
    through it. The prompt says "copy it to your output and edit", and an agent that edits
    in place instead would silently rewrite the archived record of what the gate rejected
    (I2) while leaving ``output/`` empty. The archive is the one thing in a step that must
    not change, so this tree is the one place that must not be linked.
    """
    src = workdir / "attempts" / str(archived) / "output"
    if not src.is_dir() or not any(src.iterdir()):
        return None
    dst = workdir / "input" / "_rejected"
    if dst.exists():
        shutil.rmtree(long_path(dst))
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        if child.is_dir():
            shutil.copytree(long_path(child), long_path(dst / child.name))
        else:
            shutil.copy2(long_path(child), long_path(dst / child.name))
    return "input/_rejected"


def _resume_retry_context(workdir: Path) -> tuple[str | None, str | None]:
    """Rebuild the retry context after a resume: ``(rejected_dir, gate_feedback)``.

    A resumed step re-enters ``execute_agent_step`` with ``tries`` back at 0 and both
    locals gone, and ``_materialize`` wipes ``input/`` — so the rejected document and the
    feedback that names its faults both vanish, and the agent writes the artifact again
    from its sources. That is the very rewrite this mechanism exists to prevent, and it
    would happen precisely on the runs that already cost the most.

    Everything needed is on disk in ``attempts/<n>/``: rebuild from the newest archive.
    """
    # The workdir root first. An attempt is archived at the START of the next iteration, so
    # a step that died after a gate failure — the exact shape of an interrupted retry —
    # leaves its rejected output and report sitting in the root, never archived.
    root = _retry_context_from(workdir)
    if root != (None, None):
        return root
    attempts = workdir / "attempts"
    if not attempts.is_dir():
        return None, None
    numbers = sorted(
        (int(d.name) for d in attempts.iterdir() if d.is_dir() and d.name.isdigit()),
        reverse=True,
    )
    for n in numbers:
        rejected = _rejected_input(workdir, n)
        if rejected is None:
            continue  # that attempt produced nothing; try the one before it
        return rejected, _feedback_from(attempts / str(n) / "gate_report.json")
    return None, None


def _feedback_from(report_path: Path) -> str | None:
    """The gate's own wording for a previous attempt, or ``None`` if unreadable."""
    if not report_path.exists():
        return None
    try:
        report = GateReport.model_validate_json(report_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return _format_feedback(report) or None


def _retry_context_from(workdir: Path) -> tuple[str | None, str | None]:
    """Retry context from an UNARCHIVED attempt left in the workdir root (SPEC §10.2)."""
    report_path = workdir / "gate_report.json"
    output = workdir / "output"
    if not report_path.exists() or not output.is_dir() or not any(output.iterdir()):
        return None, None
    feedback = _feedback_from(report_path)
    if feedback is None:
        return None, None  # the gate passed, or the report is unreadable: nothing rejected
    # Archive it now, under the next free slot, so the copy handed back is a copy of a
    # record rather than of a live directory the step is about to overwrite.
    n = _next_attempt(workdir)
    _archive_attempt(workdir, n)
    output.mkdir(parents=True, exist_ok=True)
    return _rejected_input(workdir, n), feedback


def _archive_attempt(workdir: Path, n: int) -> None:
    """Move the completed attempt's artifacts to ``attempts/<n>/`` (SPEC §10.2)."""
    dest = workdir / "attempts" / str(n)
    # never nest into or clobber an existing archive (resume / re-exec safety)
    if dest.exists() and any(dest.iterdir()):
        raise RuntimeError(f"attempt archive already populated: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    # I9 wants each attempt's record self-contained, and the archived `prompt.md` names
    # `input/_rejected/` — a directory the NEXT retry overwrites. Record what was actually
    # in it, so reading attempt 3 does not silently describe attempt 4's inputs.
    rejected = workdir / "input" / "_rejected"
    if rejected.is_dir():
        names = sorted(c.name for c in rejected.iterdir())
        text = "\n".join(names) + "\n"
        (dest / "rejected_inputs.txt").write_text(text, "utf-8")
    for name in _ARCHIVED:
        src = workdir / name
        if src.exists():
            shutil.move(str(src), str(dest / name))
    output = workdir / "output"
    if output.exists():
        shutil.move(str(output), str(dest / "output"))


def _next_attempt(workdir: Path) -> int:
    """Smallest free ``attempts/<n>`` index (1-based)."""
    attempts = workdir / "attempts"
    n = 1
    while (attempts / str(n)).exists():
        n += 1
    return n


def _system_prompt(agent_dir: Path) -> str:
    path = agent_dir / "prompt.md"
    return path.read_text("utf-8") if path.exists() else ""


# --- the lifecycle ----------------------------------------------------------


async def execute_agent_step(
    plan: AgentStepPlan,
    runtime: AgentRuntime,
    ledger: Ledger,
    *,
    on_event: EventCallback | None = None,
    clock: Callable[[], str] = _utcnow_iso,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> StepState:
    """Run one agent step to a terminal outcome, persisting to the ledger (§10.2)."""
    emit = on_event or (lambda _e: None)
    workdir = Path(plan.workdir)
    output_dir = workdir / "output"
    input_root = workdir / "input"

    started_at = clock()
    ledger.set_step(
        plan.step_id,
        node=plan.node_id,
        status=StepStatus.running,
        tries=0,
        started_at=started_at,
    )
    emit(
        {
            "type": "step_state_changed",
            "step_id": plan.step_id,
            "payload": {"from": "pending", "to": "running"},
        }
    )

    input_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Built once: the declared inputs are the same for every gate attempt. A retry adds
    # one thing to the tree — `input/_rejected/`, the attempt the gate just refused — the
    # same way a loop round adds `input/_previous/`. It is added AFTER this call, so
    # rebuilding here would wipe it.
    _materialize(plan.inputs, input_root)

    gate_ports = _gate_ports(plan.agent, plan.registry, plan.gate_rules)
    system_prompt = _system_prompt(plan.agent_dir)

    # What this step has paid, over every attempt — including the ones whose output
    # was archived and thrown away. The adapter measured this all along; until now
    # nothing read it (SPEC §9).
    paid = Usage()

    def record_usage(raw: Mapping[str, object] | None) -> None:
        nonlocal paid
        reported = Usage.from_report(raw)
        if reported is None:
            return
        paid = paid.plus(reported)
        emit(
            {
                "type": "usage",
                "step_id": plan.step_id,
                # `call` is the paid call's ordinal within this step, so the price of
                # the attempts that did not survive is countable from events alone
                "payload": {"call": paid.calls}
                | reported.model_dump(exclude={"calls"}),
            }
        )

    def finish(
        outcome: StepOutcome, *, tries: int, error: str | None = None
    ) -> StepState:
        status = StepStatus.done if outcome is StepOutcome.ok else StepStatus.failed
        ledger.set_step(
            plan.step_id,
            node=plan.node_id,
            status=status,
            outcome=outcome,
            tries=tries,
            started_at=started_at,
            finished_at=clock(),
            error=error,
            usage=paid if paid.calls else None,
        )
        emit(
            {
                "type": "step_state_changed",
                "step_id": plan.step_id,
                "payload": {
                    "from": "running",
                    "to": status.value,
                    "outcome": outcome.value,
                },
            }
        )
        step = ledger.get_step(plan.step_id)
        assert step is not None
        return step

    # --- HITL (SPEC §10.2 step 4 / §16.9) ---
    # An answer supplied by a human (via `refract answer` / the API) lands at
    # hitl/answer.json before the step is re-run; fold it into the prompt so the
    # agent proceeds instead of re-asking. The prior (question) attempt is archived.
    hitl_dir = workdir / "hitl"
    answer_path = hitl_dir / "answer.json"
    question_path = hitl_dir / "question.json"
    hitl_context: str | None = None
    if answer_path.exists():
        try:
            answer = json.loads(answer_path.read_text("utf-8")).get("answer", "")
            prior_q = (
                json.loads(question_path.read_text("utf-8")).get("question", "")
                if question_path.exists()
                else ""
            )
            hitl_context = (
                "## Human clarification\n"
                f"Earlier you asked: {prior_q}\n"
                f"The human answered: {answer}\n"
                "Use this answer and produce the required output now — do not ask "
                "again unless genuinely still blocked."
            )
        except (OSError, json.JSONDecodeError):
            hitl_context = None
        if output_dir.exists() and any(output_dir.iterdir()):
            _archive_attempt(workdir, _next_attempt(workdir))
            output_dir.mkdir(parents=True, exist_ok=True)
        answer_path.unlink(missing_ok=True)  # consumed for this turn

    def finish_waiting(question: dict[str, object], *, tries: int) -> StepState:
        hitl_dir.mkdir(parents=True, exist_ok=True)
        question_path.write_text(
            json.dumps(question, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ledger.set_step(
            plan.step_id,
            node=plan.node_id,
            status=StepStatus.waiting_human,
            outcome=None,
            tries=tries,
            started_at=started_at,
            # the turn that produced the question was paid for; a parked step that
            # showed no cost would understate the run every time a human is asked
            usage=paid if paid.calls else None,
        )
        emit(
            {
                "type": "step_state_changed",
                "step_id": plan.step_id,
                "payload": {"from": "running", "to": "waiting_human"},
            }
        )
        emit(
            {
                "type": "question",
                "step_id": plan.step_id,
                "payload": {
                    # `kind` tells a client which control to offer: free text for an
                    # agent's question, approve/reject for a capability request, a
                    # continue button for a checkpoint (§16.9/§16.10/§21)
                    "kind": "hitl",
                    "question": question.get("question", ""),
                    "context": question.get("context", ""),
                    "options": question.get("options", []),
                },
            }
        )
        step = ledger.get_step(plan.step_id)
        assert step is not None
        return step

    # tries counts COMPLETED gate attempts; timeout/failed_infra report the
    # count reached before the terminal failure (0 if it never completed a run).
    tries = 0
    # A resume re-enters here with the counters reset, so the retry context is rebuilt from
    # the archives rather than assumed absent.
    rejected, gate_feedback = _resume_retry_context(workdir)
    while True:
        if tries > 0:  # gate retry: archive the prior completed attempt first
            # allocate the next free slot, not `tries` — a HITL answer-resume may
            # have already consumed attempts/1 for the parked question turn.
            archived = _next_attempt(workdir)
            _archive_attempt(workdir, archived)
            output_dir.mkdir(parents=True, exist_ok=True)
            # Hand the REJECTED attempt back as an input. Archiving moved `output/` away,
            # so without this the agent starts from nothing and writes the document again
            # from its sources — when the feedback said "remove 456 characters". Measured
            # on a live article: three attempts at one step cost $11.00 of which $9.17 was
            # spent re-composing 11 000 characters that were already almost right, and the
            # feedback is worded as an edit ("this revision must end shorter than it
            # started") which is unanswerable if there is nothing to shorten.
            rejected = _rejected_input(workdir, archived)

        task_prompt = build_task_prompt(
            agent=plan.agent,
            registry=plan.registry,
            workdir=workdir,
            revision=plan.revision,
            gate_feedback=gate_feedback,
            rejected_dir=rejected,
            gate_rules=plan.gate_rules,
        )
        if hitl_context is not None:
            task_prompt = f"{task_prompt}\n{hitl_context}\n"
        full_prompt = (
            f"{system_prompt}\n\n{task_prompt}" if system_prompt else task_prompt
        )
        (workdir / "prompt.md").write_text(full_prompt, encoding="utf-8")

        spec = StepSpec(
            step_id=plan.step_id,
            agent_dir=plan.agent_dir,
            model=plan.model,
            workdir=workdir,
            prompt=task_prompt,
            system_prompt=system_prompt,
            needs=list(plan.agent.needs),
            env=list(plan.agent.env),
            timeout_s=plan.timeout_s,
        )

        # step 3: run with timeout + infra-error backoff (separate counter)
        result = await _run_with_infra_retries(
            runtime, spec, plan.infra_retries, emit, sleeper, record_usage
        )
        if result is None:
            return finish(StepOutcome.timeout, tries=tries, error="timeout")
        if not result.completed:
            # keep the adapter's reason when it gave one: "infra retries exhausted"
            # alone sent a reader looking for an engine bug when the provider had
            # been answering 429 all along
            detail = result.agent_error or "infra retries exhausted"
            return finish(StepOutcome.failed_infra, tries=tries, error=detail)

        tries += 1

        if result.agent_error is not None:
            return finish(
                StepOutcome.failed_agent, tries=tries, error=result.agent_error
            )

        # step 4: HITL — a valid question@v1 pauses the step for a human (§16.9).
        question = _read_question(plan.agent, plan.registry, output_dir)
        if question is not None:
            return finish_waiting(question, tries=tries)

        # step 5: gate (schema + rules), then any step-specific extra validation
        report = run_gate(output_dir, gate_ports)
        write_gate_report(workdir, report)
        extra = plan.extra_gate(output_dir) if (report.ok and plan.extra_gate) else []
        if report.ok and not extra:
            return finish(StepOutcome.ok, tries=tries)
        feedback = "\n".join(filter(None, [_format_feedback(report), "\n".join(extra)]))
        if tries >= plan.gate_retries + 1:
            return finish(StepOutcome.failed_validation, tries=tries, error=feedback)
        gate_feedback = feedback


async def _run_with_infra_retries(
    runtime: AgentRuntime,
    spec: StepSpec,
    infra_retries: int,
    emit: EventCallback,
    sleeper: Callable[[float], Awaitable[None]],
    record_usage: Callable[[Mapping[str, object] | None], None],
) -> StepResult | None:
    """Run the step; retry infra errors with backoff. None means timeout (§10.2).

    Every call is reported to ``record_usage``, failed ones included: an infra retry
    can burn tokens before the connection breaks, and a budget that only counts
    successful calls understates the run.
    """
    infra_attempt = 0
    while True:
        try:
            result = await asyncio.wait_for(
                runtime.run_step(spec, emit), timeout=spec.timeout_s
            )
        except asyncio.TimeoutError:
            return None
        record_usage(result.usage)
        if result.completed:
            return result
        infra_attempt += 1
        if infra_attempt > infra_retries:
            return result
        # a step that retried three times used to look exactly like one that ran once:
        # the wait, the reason and the count left no trace anywhere (SPEC §9)
        emit(
            {
                "type": "log",
                "step_id": spec.step_id,
                "payload": {
                    "infra_retry": {
                        "attempt": infra_attempt,
                        "of": infra_retries,
                        "delay_s": _backoff_delay(infra_attempt),
                        "reason": result.agent_error or "no reason reported",
                    }
                },
            }
        )
        await sleeper(_backoff_delay(infra_attempt))
