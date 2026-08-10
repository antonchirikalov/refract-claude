"""Post-mortem of a run: what it cost, what broke, and where the cause was (SPEC §14).

Reads only what the engine already wrote — ``state.json``, ``events.jsonl`` and the
per-step ``gate_report.json`` — and keeps no state of its own (I7).

Every live-run debugging story in this project's log reads the same way: the run failed
two steps from the cause. A discover step wrote its shelf outside the workdir and the
node was condemned to fail its gate later; a telemetry event with an unknown type killed
a step that had already passed its gate; a limit message the classifier did not know
burned a whole map node in seconds. In each case the ledger held the answer and nobody
could see it without reading JSONL by hand. This module is that reading, done once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from refract.models.ledger import (
    Event,
    EventType,
    NodeStatus,
    RunState,
    StepStatus,
    Usage,
)
from refract.state import STATE_FILENAME, step_workdir

# A gate that passed within this fraction of its floor is reported: clearing a 20 000
# character minimum at 20 100 is a different artifact from clearing it at 80 000, and
# the binary verdict says the same thing about both.
THIN_MARGIN = 0.05
# A file that passed the gate at fewer bytes than this is worth a second look: the type
# had no rules to state a floor, so "not empty" was the only bar it had to clear.
THIN_BYTES = 400


@dataclass(frozen=True)
class RootCause:
    """The first step that failed, in the order the run actually reached them."""

    step_id: str
    node: str
    outcome: str | None
    error: str | None


@dataclass(frozen=True)
class StepSpend:
    step_id: str
    node: str
    usage: Usage
    # cost of the attempts that did not survive: gate retries whose output was
    # archived, and every attempt of a step that failed in the end
    wasted_usd: float


@dataclass(frozen=True)
class Retry:
    step_id: str
    attempt: int
    reason: str


@dataclass(frozen=True)
class ThinPass:
    """A gate that passed, but only just — the class of pass worth looking at."""

    step_id: str
    port: str
    detail: str


@dataclass(frozen=True)
class Diagnosis:
    run_id: str
    status: str
    pipeline: str
    nodes: dict[str, int] = field(default_factory=dict)
    steps: dict[str, int] = field(default_factory=dict)
    total: Usage = field(default_factory=Usage)
    wasted: Usage = field(default_factory=Usage)
    by_node: dict[str, Usage] = field(default_factory=dict)
    spend: list[StepSpend] = field(default_factory=list)
    root_cause: RootCause | None = None
    fallout: list[str] = field(default_factory=list)
    retries: list[Retry] = field(default_factory=list)
    failed_mcp: list[str] = field(default_factory=list)
    unused_mcp: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    thin: list[ThinPass] = field(default_factory=list)


def read_events(run_dir: Path | str) -> list[Event]:
    """Parse ``events.jsonl``; unparseable or unknown-typed lines are skipped.

    Never raises on a bad line: a post-mortem of a broken run is exactly the moment a
    truncated last line is likely, and refusing to explain the run because of it would
    repeat the mistake telemetry already made once (an unknown event type must not
    decide whether work stood).
    """
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    events: list[Event] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(Event.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


def diagnose(run_dir: Path | str) -> Diagnosis:
    """Analyse a finished or running run (SPEC §14)."""
    run_dir = Path(run_dir)
    state_file = run_dir / STATE_FILENAME
    if not state_file.exists():
        raise FileNotFoundError(f"no {STATE_FILENAME} in {run_dir}")
    state = RunState.model_validate(json.loads(state_file.read_text("utf-8")))
    events = read_events(run_dir)

    node_counts: dict[str, int] = {}
    for node in state.nodes.values():
        node_counts[node.status.value] = node_counts.get(node.status.value, 0) + 1
    step_counts: dict[str, int] = {}
    for step in state.steps.values():
        step_counts[step.status.value] = step_counts.get(step.status.value, 0) + 1

    per_call = _usage_events_by_step(events)
    total = Usage()
    wasted = Usage()
    by_node: dict[str, Usage] = {}
    spend: list[StepSpend] = []
    for step_id, step in state.steps.items():
        if step.usage is None:
            continue
        total = total.plus(step.usage)
        by_node[step.node] = by_node.get(step.node, Usage()).plus(step.usage)
        step_wasted = _wasted(step.status, step.usage, per_call.get(step_id, []))
        wasted = wasted.plus(step_wasted)
        spend.append(
            StepSpend(
                step_id=step_id,
                node=step.node,
                usage=step.usage,
                wasted_usd=step_wasted.cost_usd,
            )
        )
    spend.sort(key=lambda s: (-s.usage.cost_usd, -s.usage.calls, s.step_id))

    return Diagnosis(
        run_id=state.run_id,
        status=state.status.value,
        pipeline=state.pipeline,
        nodes=node_counts,
        steps=step_counts,
        total=total,
        wasted=wasted,
        by_node=by_node,
        spend=spend,
        root_cause=_root_cause(state, events),
        fallout=[
            nid
            for nid, node in state.nodes.items()
            if node.status is NodeStatus.skipped
        ],
        retries=_retries(events),
        failed_mcp=_log_names(events, "failed_mcp_servers"),
        unused_mcp=_log_names(events, "unused_mcp_servers"),
        warnings=_warnings(events),
        thin=_thin_passes(run_dir, state),
    )


# --- money ------------------------------------------------------------------


def _usage_events_by_step(events: list[Event]) -> dict[str, list[Usage]]:
    """Per-call costs, in order, keyed by step id (from the event stream, I7)."""
    calls: dict[str, list[Usage]] = {}
    for event in events:
        if event.type is not EventType.usage or event.step_id is None:
            continue
        payload = dict(event.payload)
        payload.pop("call", None)
        try:
            usage = Usage.model_validate(payload)
        except ValueError:
            continue
        calls.setdefault(event.step_id, []).append(usage)
    return calls


def _wasted(status: StepStatus, total: Usage, calls: list[Usage]) -> Usage:
    """What was paid for work that did not survive.

    A failed step wasted everything it spent — nothing of it reached a downstream
    node. A step that succeeded on its Nth attempt wasted the first N-1: their output
    was archived to ``attempts/`` and replaced. With no per-call events (an older run,
    or a runtime that reports nothing) only the failed case can be stated.
    """
    if status is StepStatus.failed:
        return total
    if len(calls) <= 1:
        return Usage()
    spent = Usage()
    for call in calls[:-1]:
        spent = spent.plus(call)
    return spent


# --- causes -----------------------------------------------------------------


def _root_cause(state: RunState, events: list[Event]) -> RootCause | None:
    """The FIRST failure in chronological order, not the first in the ledger's dict.

    Order comes from the event stream, because that is the only record of when a step
    reached its end; the ledger is a mapping and its iteration order is insertion
    order of ids, which for a map node is the order elements were scheduled.
    """
    failed = {
        sid: step
        for sid, step in state.steps.items()
        if step.status is StepStatus.failed
    }
    if not failed:
        return None
    for event in events:
        if (
            event.type is EventType.step_state_changed
            and event.step_id in failed
            and str(event.payload.get("to")) == StepStatus.failed.value
        ):
            step = failed[event.step_id]
            assert event.step_id is not None
            return RootCause(
                step_id=event.step_id,
                node=step.node,
                outcome=step.outcome.value if step.outcome else None,
                error=step.error,
            )
    sid, step = next(iter(failed.items()))
    return RootCause(
        step_id=sid,
        node=step.node,
        outcome=step.outcome.value if step.outcome else None,
        error=step.error,
    )


def _retries(events: list[Event]) -> list[Retry]:
    out: list[Retry] = []
    for event in events:
        if event.type is not EventType.log:
            continue
        record = event.payload.get("infra_retry")
        if isinstance(record, dict):
            out.append(
                Retry(
                    step_id=event.step_id or "",
                    attempt=int(record.get("attempt", 0) or 0),
                    reason=str(record.get("reason", "")),
                )
            )
    return out


def _log_names(events: list[Event], key: str) -> list[str]:
    names: list[str] = []
    for event in events:
        if event.type is not EventType.log:
            continue
        value = event.payload.get(key)
        if isinstance(value, list):
            for item in value:
                if str(item) not in names:
                    names.append(str(item))
    return names


def _warnings(events: list[Event]) -> list[str]:
    out: list[str] = []
    for event in events:
        if event.type is not EventType.log:
            continue
        value = event.payload.get("warning")
        if value is None:
            continue
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        prefix = f"{event.step_id}: " if event.step_id else ""
        out.append(f"{prefix}{text}")
    return out


# --- gates that barely passed ------------------------------------------------


def _thin_passes(run_dir: Path, state: RunState) -> list[ThinPass]:
    """Ports that passed their gate close to the floor (SPEC §10.2 ``measures``)."""
    thin: list[ThinPass] = []
    for step_id, step in state.steps.items():
        if step.status is not StepStatus.done:
            continue
        report = step_workdir(run_dir, step_id) / "gate_report.json"
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for port in data.get("ports", []) if isinstance(data, dict) else []:
            if not isinstance(port, dict) or not port.get("ok"):
                continue
            detail = _thin_detail(port.get("measures"))
            if detail is not None:
                thin.append(
                    ThinPass(
                        step_id=step_id, port=str(port.get("port", "")), detail=detail
                    )
                )
    return thin


def _thin_detail(measures: object) -> str | None:
    if not isinstance(measures, dict):
        return None
    chars = measures.get("chars")
    floor = measures.get("min_length")
    if isinstance(chars, int) and isinstance(floor, int) and floor > 0:
        if chars < floor * (1 + THIN_MARGIN):
            over = (chars / floor - 1) * 100
            return f"{chars} chars against a {floor} floor (+{over:.1f}%)"
    entries = measures.get("entries")
    if isinstance(entries, int) and entries == 1:
        return "directory passed with a single entry"
    size = measures.get("bytes")
    if isinstance(size, int) and 0 < size < THIN_BYTES:
        return f"{size} bytes, and the type states no floor"
    citations = measures.get("citations")
    if isinstance(citations, dict):
        shortest = citations.get("shortest_entry")
        least = citations.get("min_entry_chars")
        if (
            isinstance(shortest, int)
            and isinstance(least, int)
            and shortest < least * 1.1
        ):
            return f"shortest source entry {shortest} chars against a {least} floor"
    return None


# --- rendering ---------------------------------------------------------------


def _money(usage: Usage) -> str:
    if usage.cost_usd:
        return f"${usage.cost_usd:.4f}"
    if usage.calls:
        return f"{usage.calls} call(s), cost not reported"
    return "nothing"


def render(diagnosis: Diagnosis) -> str:
    """Human-readable post-mortem.

    The frame is ASCII, as elsewhere in the CLI (a run's stdout is read on Windows
    consoles whose code page is not UTF-8); quoted errors are passed through as the
    provider wrote them, exactly as ``refract status`` does.
    """
    d = diagnosis
    lines = [
        f"run:      {d.run_id}",
        f"pipeline: {d.pipeline}",
        f"status:   {d.status}",
        "nodes:    " + (_counts(d.nodes) or "(none)"),
        "steps:    " + (_counts(d.steps) or "(none)"),
    ]

    lines.append("")
    lines.append("cost:")
    if not d.total.calls:
        # a run from before usage accounting, or a runtime that reports none: say so
        # rather than print a confident zero
        lines.append("  total    not recorded (no runtime reported usage)")
    else:
        lines.append(f"  total    {_money(d.total)} over {d.total.calls} paid call(s)")
    if d.total.input_tokens or d.total.output_tokens:
        lines.append(
            f"  tokens   in {d.total.input_tokens}, out {d.total.output_tokens}, "
            f"cache read {d.total.cache_read_tokens}"
        )
    if d.wasted.calls or d.wasted.cost_usd:
        lines.append(
            f"  wasted   {_money(d.wasted)} on attempts that did not survive "
            "(failed steps and archived gate retries)"
        )
    for spend in d.spend[:5]:
        note = f", wasted {spend.wasted_usd:.4f}" if spend.wasted_usd else ""
        lines.append(
            f"  {spend.step_id:<28} {_money(spend.usage)} "
            f"({spend.usage.calls} call(s){note})"
        )

    if d.root_cause is not None:
        rc = d.root_cause
        lines.append("")
        lines.append("first failure:")
        lines.append(f"  step     {rc.step_id} (node {rc.node})")
        lines.append(f"  outcome  {rc.outcome or 'unknown'}")
        if rc.error:
            lines.append(f"  error    {_one_line(rc.error)}")
        if d.fallout:
            lines.append(f"  fallout  skipped: {', '.join(sorted(d.fallout))}")

    if d.retries:
        lines.append("")
        lines.append("infra retries:")
        for retry in d.retries:
            lines.append(
                f"  {retry.step_id:<28} attempt {retry.attempt}: "
                f"{_one_line(retry.reason)}"
            )

    if d.failed_mcp or d.unused_mcp:
        lines.append("")
        lines.append("mcp:")
        if d.failed_mcp:
            lines.append(
                f"  never connected: {', '.join(d.failed_mcp)} "
                "(declared by the agent, so the work it enables did not happen)"
            )
        if d.unused_mcp:
            lines.append(f"  never called:    {', '.join(d.unused_mcp)}")

    if d.thin:
        lines.append("")
        lines.append("passed, but only just:")
        for item in d.thin:
            lines.append(f"  {item.step_id:<28} {item.port}: {item.detail}")

    if d.warnings:
        lines.append("")
        lines.append("warnings:")
        for warning in d.warnings:
            lines.append(f"  {_one_line(warning)}")

    return "\n".join(lines)


def as_dict(diagnosis: Diagnosis) -> dict[str, object]:
    """The same diagnosis as JSON (``--json``), for scripts and the UI."""
    d = diagnosis
    return {
        "run_id": d.run_id,
        "pipeline": d.pipeline,
        "status": d.status,
        "nodes": d.nodes,
        "steps": d.steps,
        "cost": {
            "total": d.total.model_dump(),
            "wasted": d.wasted.model_dump(),
            "by_node": {n: u.model_dump() for n, u in d.by_node.items()},
            "by_step": [
                {
                    "step_id": s.step_id,
                    "node": s.node,
                    "usage": s.usage.model_dump(),
                    "wasted_usd": s.wasted_usd,
                }
                for s in d.spend
            ],
        },
        "root_cause": None
        if d.root_cause is None
        else {
            "step_id": d.root_cause.step_id,
            "node": d.root_cause.node,
            "outcome": d.root_cause.outcome,
            "error": d.root_cause.error,
        },
        "fallout": sorted(d.fallout),
        "retries": [
            {"step_id": r.step_id, "attempt": r.attempt, "reason": r.reason}
            for r in d.retries
        ],
        "mcp": {"failed": d.failed_mcp, "unused": d.unused_mcp},
        "thin": [
            {"step_id": t.step_id, "port": t.port, "detail": t.detail} for t in d.thin
        ],
        "warnings": d.warnings,
    }


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))


def _one_line(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."
