"""MockRuntime — scripted runtime for tests (SPEC §12).

Scenario is ``dict[pattern, list[ScriptedResponse]]`` where ``pattern`` is an
fnmatch pattern over ``step_id``. Consecutive calls to the same step consume
successive list elements (a single-element list is reused for every call, which
covers gate retries that should keep failing/succeeding the same way). Writes a
stub ``raw.txt`` and a minimal ``agent.events.jsonl`` per attempt (I9). No
network, no API keys, no real CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from refract.runtime.base import EventCallback, StepResult, StepSpec


@dataclass
class ScriptedResponse:
    """One scripted step outcome (SPEC §12).

    ``files`` maps a path relative to ``output/`` to its content (text or
    bytes). ``completed=False`` simulates an infra error; ``agent_error`` marks
    an agent-execution failure. ``events`` are emitted via ``on_event`` and
    persisted to ``agent.events.jsonl``.
    """

    files: dict[str, str | bytes] = field(default_factory=dict)
    completed: bool = True
    agent_error: str | None = None
    usage: dict[str, object] | None = None
    events: list[dict[str, object]] = field(default_factory=list)


class MockRuntime:
    """A structural ``AgentRuntime`` that replays scripted responses."""

    def __init__(self, scenario: dict[str, list[ScriptedResponse]]) -> None:
        self._scenario = scenario
        self._calls: dict[str, int] = {}

    def _responses_for(self, step_id: str) -> list[ScriptedResponse]:
        for pattern, responses in self._scenario.items():
            if fnmatchcase(step_id, pattern):
                return responses
        raise ValueError(
            f"MockRuntime: no scripted response for step {step_id!r}; "
            f"patterns={list(self._scenario)}"
        )

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        responses = self._responses_for(spec.step_id)
        index = self._calls.get(spec.step_id, 0)
        self._calls[spec.step_id] = index + 1
        resp = responses[index] if index < len(responses) else responses[-1]

        # I9: the adapter writes raw.txt and agent.events.jsonl into the workdir.
        events = resp.events or [
            {"type": "log", "step_id": spec.step_id, "message": "mock run_step"}
        ]
        for event in events:
            on_event(event)
        _write_jsonl(spec.workdir / "agent.events.jsonl", events)
        (spec.workdir / "raw.txt").write_text(
            f"[mock] step={spec.step_id} model={spec.model} "
            f"completed={resp.completed} agent_error={resp.agent_error}\n",
            encoding="utf-8",
        )

        # Only a completed, non-errored step produces output artifacts.
        if resp.completed and resp.agent_error is None:
            output_dir = spec.workdir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in resp.files.items():
                target = output_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    target.write_text(content, encoding="utf-8")

        return StepResult(
            completed=resp.completed, agent_error=resp.agent_error, usage=resp.usage
        )

    async def close(self) -> None:
        return None


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
