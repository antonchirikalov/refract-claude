"""Run ledger (``state.json``) and events (``events.jsonl``) formats (SPEC §9).

Two ledger levels — nodes and steps. Enums cover the step/node/run status
machines and the step outcome taxonomy. ``state.json`` is written only by the
engine, only atomically (I3); models here are the format contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    waiting_human = "waiting_human"  # phase 3
    cancelled = "cancelled"
    reused = "reused"


class StepOutcome(str, Enum):
    ok = "ok"
    failed_validation = "failed_validation"
    failed_agent = "failed_agent"
    failed_infra = "failed_infra"
    timeout = "timeout"


class NodeStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"
    reused = "reused"
    waiting_human = "waiting_human"  # phase 3


class RunStatus(str, Enum):
    created = "created"
    validating = "validating"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    waiting_human = "waiting_human"  # phase 3


class Usage(BaseModel):
    """What a step's LLM work cost (SPEC §9).

    Accumulated over ALL paid calls of the step: gate retries, infra retries and the
    attempts whose output was archived and thrown away. A budget cares about what was
    paid, not about what was paid for the attempt that happened to pass the gate — and
    a live run once paid four times over for a shelf three quarters of which was never
    read, with nothing in the ledger to show it.

    The adapter already measures this per step (the CLI's ``result`` frame carries cost,
    tokens and duration); before this existed the engine dropped it on the floor.
    """

    model_config = ConfigDict(extra="forbid")

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    # paid runtime calls, not gate attempts: an infra retry that burned tokens counts
    calls: int = 0

    @classmethod
    def from_report(cls, raw: Mapping[str, object] | None) -> "Usage | None":
        """Normalise one ``StepResult.usage`` report; ``None`` when nothing was reported.

        The report is a loose dict by contract (SPEC §12) — a runtime is free to know
        nothing about cost — so every field is optional and coerced defensively. A
        runtime that reports an empty dict still counts as one paid call: the call
        happened, only its price is unknown.
        """
        if raw is None:
            return None
        tokens = raw.get("tokens")
        tok: Mapping[str, object] = tokens if isinstance(tokens, Mapping) else {}
        return cls(
            cost_usd=_as_float(raw.get("cost")),
            input_tokens=_as_int(tok.get("input_tokens")),
            output_tokens=_as_int(tok.get("output_tokens")),
            cache_read_tokens=_as_int(tok.get("cache_read_input_tokens")),
            cache_write_tokens=_as_int(tok.get("cache_creation_input_tokens")),
            duration_ms=_as_int(raw.get("duration_ms")),
            calls=1,
        )

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            cost_usd=self.cost_usd + other.cost_usd,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            duration_ms=self.duration_ms + other.duration_ms,
            calls=self.calls + other.calls,
        )


def _as_float(value: object) -> float:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def _as_int(value: object) -> int:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0
    )


class NodeState(BaseModel):
    """Ledger record for a node (SPEC §9). ``winner*`` set by select nodes."""

    model_config = ConfigDict(extra="forbid")

    status: NodeStatus
    error: str | None = None
    winner: str | None = None
    winner_model: str | None = None


class StepState(BaseModel):
    """Ledger record for a step (SPEC §9)."""

    model_config = ConfigDict(extra="forbid")

    node: str
    status: StepStatus
    outcome: StepOutcome | None = None
    tries: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    # what this step paid, over all its attempts; absent for builtin and reused steps
    # (nothing was spent) and for a runtime that reports no usage at all
    usage: Usage | None = None


class RunState(BaseModel):
    """``state.json`` (SPEC §9)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    pipeline: str
    created_at: str
    finished_at: str | None = None
    reuse_from: str | None = None
    force_nodes: list[str] = Field(default_factory=list)
    # Node whose checkpoint parked this run (SPEC §21); cleared when it continues.
    awaiting_checkpoint: str | None = None
    nodes: dict[str, NodeState] = Field(default_factory=dict)
    steps: dict[str, StepState] = Field(default_factory=dict)


class EventType(str, Enum):
    run_state_changed = "run_state_changed"
    step_state_changed = "step_state_changed"
    node_state_changed = "node_state_changed"
    heartbeat = "heartbeat"
    tool_call = "tool_call"
    log = "log"
    question = "question"  # phase 3
    # one per PAID runtime call, so a run's cost is reconstructible from the event
    # stream alone (I7) and the price of thrown-away attempts stays visible
    usage = "usage"


class Event(BaseModel):
    """One ``events.jsonl`` record (SPEC §9). ``seq`` assigned by the writer."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: str
    type: EventType
    step_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
