"""AgentRuntime protocol, StepSpec, StepResult (SPEC §12).

The engine hands a ``StepSpec`` to a runtime and evaluates the result by the
files the agent wrote under ``workdir/output/`` (the gate, §10.2) — NOT by
``StepResult``. Per I9 the adapter is responsible for writing ``raw.txt`` and
``agent.events.jsonl`` into the workdir; the engine writes ``prompt.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# An event is a plain dict (SPEC §9 event payloads); the engine assigns seq.
EventCallback = Callable[[dict[str, object]], None]


@dataclass
class StepSpec:
    """Everything a runtime needs to execute one step (SPEC §12)."""

    step_id: str
    agent_dir: Path  # package copied into snapshot/agents/
    model: str  # "provider/model-id"
    workdir: Path
    prompt: str  # task prompt (§11 items 2–4); system prompt is in the package
    system_prompt: str  # contents of the agent's prompt.md
    needs: list[str]
    # Environment variable NAMES the agent declared (SPEC §6, I8). The runtime passes
    # these through from its own environment; values never travel in the spec.
    env: list[str] = field(default_factory=list)
    timeout_s: int = 3600


@dataclass
class StepResult:
    """Outcome reported by the runtime (SPEC §12).

    ``completed=False`` signals an infra error the engine should retry.
    ``agent_error`` (with ``completed=True``) means the agent itself failed →
    ``failed_agent``. Success/failure of the *work* is judged by the gate, not
    by this object.
    """

    completed: bool
    agent_error: str | None = None
    usage: dict[str, object] | None = None


class AgentRuntime(Protocol):
    """The runtime interface (SPEC §12)."""

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        """Execute one step, emitting events via ``on_event`` (heartbeats etc.)."""
        ...

    async def close(self) -> None:
        """Release resources; processes started by the adapter MUST be killed."""
        ...
