"""Tests for MockRuntime and the AgentRuntime protocol (SPEC §12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.runtime import (
    AgentRuntime,
    MockRuntime,
    ScriptedResponse,
    StepSpec,
)


def _spec(workdir: Path, step_id: str = "extract:rfp-doc") -> StepSpec:
    workdir.mkdir(parents=True, exist_ok=True)
    return StepSpec(
        step_id=step_id,
        agent_dir=workdir / "agent",
        model="kimi/kimi-k3",
        workdir=workdir,
        prompt="task prompt",
        system_prompt="system prompt",
        env=[],
            needs=["read"],
        timeout_s=3600,
    )


def test_mock_is_agent_runtime(tmp_path: Path) -> None:
    runtime: AgentRuntime = MockRuntime({"*": [ScriptedResponse()]})
    assert runtime is not None


async def test_writes_output_raw_and_events(tmp_path: Path) -> None:
    runtime = MockRuntime(
        {"extract:*": [ScriptedResponse(files={"extract.json": '{"ok": true}'})]}
    )
    events: list[dict[str, object]] = []
    spec = _spec(tmp_path / "wd")
    result = await runtime.run_step(spec, events.append)

    assert result.completed is True
    assert result.agent_error is None
    assert (spec.workdir / "output" / "extract.json").read_text(
        "utf-8"
    ) == '{"ok": true}'
    assert (spec.workdir / "raw.txt").exists()
    events_file = spec.workdir / "agent.events.jsonl"
    assert events_file.exists()
    # emitted events match the persisted jsonl
    persisted = [
        json.loads(line) for line in events_file.read_text("utf-8").splitlines()
    ]
    assert persisted == events


async def test_infra_error_writes_no_output(tmp_path: Path) -> None:
    runtime = MockRuntime({"*": [ScriptedResponse(completed=False)]})
    spec = _spec(tmp_path / "wd")
    result = await runtime.run_step(spec, lambda e: None)
    assert result.completed is False
    assert not (spec.workdir / "output").exists()
    assert (spec.workdir / "raw.txt").exists()  # trace still written (I9)


async def test_agent_error_reported(tmp_path: Path) -> None:
    runtime = MockRuntime({"*": [ScriptedResponse(agent_error="boom")]})
    spec = _spec(tmp_path / "wd")
    result = await runtime.run_step(spec, lambda e: None)
    assert result.completed is True
    assert result.agent_error == "boom"
    # errored step produces no output artifacts
    assert not (spec.workdir / "output").exists()


async def test_sequential_responses_per_attempt(tmp_path: Path) -> None:
    runtime = MockRuntime(
        {
            "extract:*": [
                ScriptedResponse(files={"a.txt": "first"}),
                ScriptedResponse(files={"a.txt": "second"}),
            ]
        }
    )
    spec = _spec(tmp_path / "wd")
    await runtime.run_step(spec, lambda e: None)
    assert (spec.workdir / "output" / "a.txt").read_text("utf-8") == "first"
    await runtime.run_step(spec, lambda e: None)
    assert (spec.workdir / "output" / "a.txt").read_text("utf-8") == "second"


async def test_single_response_reused_after_exhaustion(tmp_path: Path) -> None:
    runtime = MockRuntime({"*": [ScriptedResponse(files={"a.txt": "same"})]})
    spec = _spec(tmp_path / "wd")
    await runtime.run_step(spec, lambda e: None)
    r2 = await runtime.run_step(spec, lambda e: None)
    assert r2.completed is True
    assert (spec.workdir / "output" / "a.txt").read_text("utf-8") == "same"


async def test_unmatched_step_raises(tmp_path: Path) -> None:
    runtime = MockRuntime({"scan:*": [ScriptedResponse()]})
    spec = _spec(tmp_path / "wd", step_id="extract:rfp")
    with pytest.raises(ValueError, match="no scripted response"):
        await runtime.run_step(spec, lambda e: None)


async def test_bytes_and_nested_paths(tmp_path: Path) -> None:
    runtime = MockRuntime(
        {"*": [ScriptedResponse(files={"sub/dir/x.bin": b"\x00\x01"})]}
    )
    spec = _spec(tmp_path / "wd")
    await runtime.run_step(spec, lambda e: None)
    assert (
        spec.workdir / "output" / "sub" / "dir" / "x.bin"
    ).read_bytes() == b"\x00\x01"
