"""Run ledger: state.json load / save / mutations (SPEC §9).

Two levels — nodes and steps. ``state.json`` is written ONLY here and ONLY
atomically (tmp file + ``os.replace``), one write per change (I3). On load,
``running`` steps and nodes become ``pending`` — the crash-recovery mechanism
(SPEC §9); do not optimize it away.

Timestamps are passed in by callers (not read from a clock here) so runs stay
deterministic and testable.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from refract.models.ledger import (
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    StepOutcome,
    StepState,
    StepStatus,
    Usage,
)

STATE_FILENAME = "state.json"
_TMP_SUFFIX = ".tmp"


_REPLACE_ATTEMPTS = 25
_REPLACE_BACKOFF_S = 0.02


def read_state(run_dir: Path | str) -> dict[str, object]:
    """Read ``state.json``, tolerating a write in flight (SPEC §9).

    The writer replaces the file atomically, but a reader can still catch the moment
    between two handles and see nothing at all, so an empty/partial read is retried
    rather than reported as corruption.
    """
    path = Path(run_dir) / STATE_FILENAME
    last: Exception | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            data = json.loads(path.read_text("utf-8"))
            assert isinstance(data, dict)
            return data
        except (json.JSONDecodeError, PermissionError, OSError) as exc:
            last = exc
            if attempt == _REPLACE_ATTEMPTS - 1:
                break
            time.sleep(_REPLACE_BACKOFF_S)
    raise RuntimeError(f"could not read {path}: {last}")


def step_workdir(run_dir: Path, step_id: str) -> Path:
    """Directory of a step, from its ledger id (SPEC §9/§10.2/§10.3).

    The one place that knows the naming, because a step id is not a path:
    ``scan`` → ``steps/scan/main``, ``extract:rfp-md`` → ``steps/extract/rfp-md``,
    ``refine.body:r1`` → ``steps/refine/body_r1``, ``choose.selector`` →
    ``steps/choose/selector``. The API used to guess and so could not reach the
    artifacts of map elements or loop rounds at all.
    """
    head, _, leaf = step_id.partition(":")
    node, _, block = head.partition(".")
    base = Path(run_dir) / "steps" / node
    if block and leaf:  # loop sub-step: body/critic of a round
        return base / f"{block}_{leaf}"
    if block:  # select's selector
        return base / block
    if leaf:  # map element
        return base / leaf
    return base / "main"


class Ledger:
    """Owns a run's ``state.json`` and is the only writer of it (I3)."""

    def __init__(self, run_dir: Path | str, state: RunState) -> None:
        self.run_dir = Path(run_dir)
        self.state = state

    @property
    def path(self) -> Path:
        return self.run_dir / STATE_FILENAME

    # --- construction / persistence ----------------------------------------

    @classmethod
    def create(
        cls,
        run_dir: Path | str,
        *,
        run_id: str,
        pipeline: str,
        node_ids: list[str],
        created_at: str,
        reuse_from: str | None = None,
        force_nodes: list[str] | None = None,
        status: RunStatus = RunStatus.created,
    ) -> "Ledger":
        """Create a fresh ledger with every node ``pending`` and no steps yet."""
        state = RunState(
            run_id=run_id,
            status=status,
            pipeline=pipeline,
            created_at=created_at,
            reuse_from=reuse_from,
            force_nodes=list(force_nodes or []),
            nodes={nid: NodeState(status=NodeStatus.pending) for nid in node_ids},
            steps={},
        )
        ledger = cls(run_dir, state)
        ledger.save()
        return ledger

    @classmethod
    def load(cls, run_dir: Path | str) -> "Ledger":
        """Load ``state.json`` and apply crash recovery (``running → pending``)."""
        run_dir = Path(run_dir)
        raw = read_state(run_dir)
        state = RunState.model_validate(raw)
        ledger = cls(run_dir, state)
        if ledger._recover_running():
            ledger.save()
        return ledger

    def _recover_running(self) -> bool:
        """running → pending for steps and nodes (SPEC §9). Returns True if changed."""
        changed = False
        for step in self.state.steps.values():
            if step.status is StepStatus.running:
                step.status = StepStatus.pending
                changed = True
        for node in self.state.nodes.values():
            if node.status is NodeStatus.running:
                node.status = NodeStatus.pending
                changed = True
        return changed

    def save(self) -> None:
        """Atomically write ``state.json`` (tmp + os.replace, UTF-8) — I3.

        The replace is retried briefly: on Windows it raises PermissionError while a
        reader holds the destination open, and readers are constant (a UI polls this
        file). Losing a whole run because a viewer looked at it at the wrong moment is
        not acceptable — and that is exactly what happened before this retry.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            self.state.model_dump(mode="json"), indent=2, ensure_ascii=False
        )
        tmp = self.run_dir / (STATE_FILENAME + _TMP_SUFFIX)
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_S)

    # --- run-level mutations -----------------------------------------------

    def set_run_status(
        self, status: RunStatus, *, finished_at: str | None = None
    ) -> None:
        self.state.status = status
        if finished_at is not None:
            self.state.finished_at = finished_at
        self.save()

    # --- node-level mutations ----------------------------------------------

    def set_node_status(
        self, node_id: str, status: NodeStatus, *, error: str | None = None
    ) -> None:
        node = self.state.nodes.setdefault(node_id, NodeState(status=status))
        node.status = status
        node.error = error
        self.save()

    def set_node_selection(
        self, node_id: str, *, winner: str | None, winner_model: str | None
    ) -> None:
        """Record select-node exports (SPEC §10.3)."""
        node = self.state.nodes.setdefault(
            node_id, NodeState(status=NodeStatus.pending)
        )
        node.winner = winner
        node.winner_model = winner_model
        self.save()

    # --- step-level mutations ----------------------------------------------

    def set_step(
        self,
        step_id: str,
        *,
        node: str,
        status: StepStatus,
        outcome: StepOutcome | None = None,
        tries: int | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        """Insert or update a step record, then persist (one atomic write, I3).

        ``usage`` is the step's accumulated cost so far and REPLACES what is stored:
        the caller accumulates over attempts, so a re-run of the same step reports its
        own total rather than adding to the previous run's.
        """
        existing = self.state.steps.get(step_id)
        if existing is None:
            existing = StepState(node=node, status=status)
            self.state.steps[step_id] = existing
        existing.node = node
        existing.status = status
        existing.outcome = outcome
        if tries is not None:
            existing.tries = tries
        if started_at is not None:
            existing.started_at = started_at
        if finished_at is not None:
            existing.finished_at = finished_at
        if usage is not None:
            existing.usage = usage
        existing.error = error
        self.save()

    def reset_failed_steps(self) -> list[str]:
        """``--retry-failed``: failed steps → pending (SPEC §10.5). Returns ids."""
        reset: list[str] = []
        for step_id, step in self.state.steps.items():
            if step.status is StepStatus.failed:
                step.status = StepStatus.pending
                step.outcome = None
                step.error = None
                reset.append(step_id)
        if reset:
            self.save()
        return reset

    # --- queries ------------------------------------------------------------

    def get_node(self, node_id: str) -> NodeState | None:
        return self.state.nodes.get(node_id)

    def get_step(self, step_id: str) -> StepState | None:
        return self.state.steps.get(step_id)

    def has_failed_nodes(self) -> bool:
        return any(n.status is NodeStatus.failed for n in self.state.nodes.values())

    def steps_for_node(self, node_id: str) -> dict[str, StepState]:
        return {sid: s for sid, s in self.state.steps.items() if s.node == node_id}

    def total_usage(self) -> Usage:
        """What this run has paid so far (SPEC §9).

        DERIVED from the step records rather than stored as a running total: a step
        re-executed by ``resume``/``rerun`` reports its own total, and a stored sum
        would have to be reconciled against that on every path. Reused steps carry no
        usage — their money was spent in the run they were reused from.
        """
        total = Usage()
        for step in self.state.steps.values():
            if step.usage is not None:
                total = total.plus(step.usage)
        return total

    def usage_by_node(self) -> dict[str, Usage]:
        """Per-node cost roll-up; nodes that paid nothing are absent."""
        by_node: dict[str, Usage] = {}
        for step in self.state.steps.values():
            if step.usage is None:
                continue
            by_node[step.node] = by_node.get(step.node, Usage()).plus(step.usage)
        return by_node

    def node_ids(self) -> list[str]:
        return list(self.state.nodes)
