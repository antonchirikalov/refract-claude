"""Tests for the run post-mortem (refract/explain.py) — SPEC §14, §9, §10.2.

The command exists because every debugging story in this project's log went the same
way: the run failed two steps from the cause, and the ledger held the answer nobody
could see without reading JSONL by hand. These tests are built from those runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.explain import as_dict, diagnose, render
from refract.models.ledger import (
    NodeStatus,
    RunStatus,
    StepOutcome,
    StepStatus,
    Usage,
)
from refract.state import Ledger

# --- builders ---------------------------------------------------------------


def _ledger(run_dir: Path, node_ids: list[str]) -> Ledger:
    return Ledger.create(
        run_dir,
        run_id="run_20260801_071001",
        pipeline="analytic_report.yaml",
        node_ids=node_ids,
        created_at="2026-08-01T07:10:01Z",
    )


def _events(run_dir: Path, records: list[dict]) -> None:
    """Write ``events.jsonl`` the way the engine's writer does (seq + ts)."""
    lines = []
    for seq, record in enumerate(records, start=1):
        line = {"seq": seq, "ts": f"T{seq}", **record}
        lines.append(json.dumps(line, ensure_ascii=False))
    (run_dir / "events.jsonl").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


def _usage_event(step_id: str, call: int, cost: float) -> dict:
    return {
        "type": "usage",
        "step_id": step_id,
        "payload": {"call": call, "cost_usd": cost, "duration_ms": 1000},
    }


def _step_failed(step_id: str) -> dict:
    return {
        "type": "step_state_changed",
        "step_id": step_id,
        "payload": {"from": "running", "to": "failed", "outcome": "failed_agent"},
    }


def _gate_report(
    run_dir: Path, step_dir: str, ports: list[dict], *, ok: bool = True
) -> None:
    path = run_dir / "steps" / step_dir
    path.mkdir(parents=True, exist_ok=True)
    (path / "gate_report.json").write_text(
        json.dumps({"ok": ok, "ports": ports}), encoding="utf-8"
    )


# --- the live-run shape: find/study done, analyse failed on a limit ----------


@pytest.fixture
def limit_run(tmp_path: Path) -> Path:
    """The 2026-08-01 run: three nodes done, the analysis killed by a weekly limit."""
    run_dir = tmp_path / "run"
    ledger = _ledger(run_dir, ["find", "study", "analyse", "report"])
    ledger.set_node_status("find", NodeStatus.done)
    ledger.set_node_status("study", NodeStatus.done)
    ledger.set_node_status("analyse", NodeStatus.failed, error="weekly limit")
    ledger.set_node_status("report", NodeStatus.skipped, error="upstream failed")
    ledger.set_step(
        "find",
        node="find",
        status=StepStatus.done,
        outcome=StepOutcome.ok,
        usage=Usage(cost_usd=1.20, calls=1, input_tokens=90_000),
    )
    ledger.set_step(
        "study:s1",
        node="study",
        status=StepStatus.done,
        outcome=StepOutcome.ok,
        tries=2,
        usage=Usage(cost_usd=0.50, calls=2),
    )
    ledger.set_step(
        "analyse",
        node="analyse",
        status=StepStatus.failed,
        outcome=StepOutcome.failed_agent,
        error="You've hit your weekly limit - resets Aug 4, 7pm (Europe/Istanbul)",
        usage=Usage(cost_usd=0.30, calls=1),
    )
    ledger.set_run_status(RunStatus.failed, finished_at="2026-08-01T09:00:00Z")
    _events(
        run_dir,
        [
            _usage_event("find", 1, 1.20),
            _usage_event("study:s1", 1, 0.20),
            _usage_event("study:s1", 2, 0.30),
            _usage_event("analyse", 1, 0.30),
            {
                "type": "log",
                "step_id": "find",
                "payload": {"failed_mcp_servers": ["pdf-reader"]},
            },
            {
                "type": "log",
                "step_id": "find",
                "payload": {"unused_mcp_servers": ["tavily-remote"]},
            },
            {
                "type": "log",
                "step_id": "study:s1",
                "payload": {
                    "infra_retry": {
                        "attempt": 1,
                        "of": 2,
                        "delay_s": 2.0,
                        "reason": "You've hit your session limit",
                    }
                },
            },
            _step_failed("analyse"),
        ],
    )
    return run_dir


class TestDiagnosis:
    def test_totals_come_from_the_ledger(self, limit_run: Path) -> None:
        d = diagnose(limit_run)
        assert d.status == "failed"
        assert d.total.cost_usd == pytest.approx(2.00)
        assert d.total.calls == 4
        assert d.by_node["study"].cost_usd == pytest.approx(0.50)
        assert d.nodes == {"done": 2, "failed": 1, "skipped": 1}

    def test_wasted_covers_failed_steps_and_archived_retries(
        self, limit_run: Path
    ) -> None:
        """0.30 of the failed analysis + 0.20 of the study attempt that was archived."""
        d = diagnose(limit_run)
        assert d.wasted.cost_usd == pytest.approx(0.50)
        by_step = {s.step_id: s.wasted_usd for s in d.spend}
        assert by_step["analyse"] == pytest.approx(0.30)
        assert by_step["study:s1"] == pytest.approx(0.20)
        assert by_step["find"] == pytest.approx(0.0)

    def test_spend_is_ranked_by_cost(self, limit_run: Path) -> None:
        d = diagnose(limit_run)
        assert [s.step_id for s in d.spend] == ["find", "study:s1", "analyse"]

    def test_root_cause_and_fallout(self, limit_run: Path) -> None:
        d = diagnose(limit_run)
        assert d.root_cause is not None
        assert d.root_cause.step_id == "analyse"
        assert d.root_cause.outcome == "failed_agent"
        assert "weekly limit" in (d.root_cause.error or "")
        assert d.fallout == ["report"]

    def test_mcp_and_retries_surface(self, limit_run: Path) -> None:
        d = diagnose(limit_run)
        assert d.failed_mcp == ["pdf-reader"]
        assert d.unused_mcp == ["tavily-remote"]
        assert len(d.retries) == 1
        assert d.retries[0].step_id == "study:s1"
        assert "session limit" in d.retries[0].reason

    def test_render_is_ascii_and_names_the_cause(self, limit_run: Path) -> None:
        text = render(diagnose(limit_run))
        text.encode("ascii")  # run output is read on cp1251 consoles
        assert "first failure:" in text
        assert "analyse" in text
        assert "wasted" in text
        assert "pdf-reader" in text

    def test_json_shape(self, limit_run: Path) -> None:
        data = as_dict(diagnose(limit_run))
        assert data["run_id"] == "run_20260801_071001"
        assert data["root_cause"]["step_id"] == "analyse"
        assert data["cost"]["total"]["cost_usd"] == pytest.approx(2.00)
        assert data["mcp"] == {"failed": ["pdf-reader"], "unused": ["tavily-remote"]}
        json.dumps(data)  # must be serialisable as-is


class TestRootCauseOrdering:
    async def test_first_failure_is_chronological_not_dict_order(
        self, tmp_path: Path
    ) -> None:
        """A map node schedules elements in id order; failures do not follow it."""
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["study"])
        for slug in ("s1", "s2"):
            ledger.set_step(
                f"study:{slug}",
                node="study",
                status=StepStatus.failed,
                outcome=StepOutcome.failed_agent,
                error=f"{slug} broke",
            )
        # s2 reached its end FIRST, even though s1 is first in the ledger mapping
        _events(run_dir, [_step_failed("study:s2"), _step_failed("study:s1")])

        d = diagnose(run_dir)
        assert d.root_cause is not None
        assert d.root_cause.step_id == "study:s2"

    def test_without_events_the_ledger_is_still_enough(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["a"])
        ledger.set_step(
            "a", node="a", status=StepStatus.failed, outcome=StepOutcome.timeout
        )
        d = diagnose(run_dir)
        assert d.root_cause is not None and d.root_cause.step_id == "a"

    def test_a_clean_run_has_no_root_cause(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["a"])
        ledger.set_step("a", node="a", status=StepStatus.done, outcome=StepOutcome.ok)
        ledger.set_node_status("a", NodeStatus.done)
        ledger.set_run_status(RunStatus.completed)
        d = diagnose(run_dir)
        assert d.root_cause is None and d.fallout == []


class TestThinPasses:
    def test_a_report_that_cleared_its_floor_by_a_hair_is_reported(
        self, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["report"])
        ledger.set_step(
            "report", node="report", status=StepStatus.done, outcome=StepOutcome.ok
        )
        _gate_report(
            run_dir,
            "report/main",
            [
                {
                    "port": "doc",
                    "ok": True,
                    "problems": [],
                    "measures": {"chars": 20_100, "min_length": 20_000},
                }
            ],
        )
        d = diagnose(run_dir)
        assert len(d.thin) == 1
        assert d.thin[0].port == "doc"
        assert "20000 floor" in d.thin[0].detail

    def test_a_comfortable_pass_is_not_reported(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["report"])
        ledger.set_step(
            "report", node="report", status=StepStatus.done, outcome=StepOutcome.ok
        )
        _gate_report(
            run_dir,
            "report/main",
            [
                {
                    "port": "doc",
                    "ok": True,
                    "problems": [],
                    "measures": {"chars": 81_000, "min_length": 20_000},
                }
            ],
        )
        assert diagnose(run_dir).thin == []

    def test_a_directory_that_passed_with_one_entry(self, tmp_path: Path) -> None:
        """A discover node that found one source passes a gate that only forbids empty."""
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["find"])
        ledger.set_step(
            "find", node="find", status=StepStatus.done, outcome=StepOutcome.ok
        )
        _gate_report(
            run_dir,
            "find/main",
            [
                {
                    "port": "sources",
                    "ok": True,
                    "problems": [],
                    "measures": {"entries": 1},
                }
            ],
        )
        d = diagnose(run_dir)
        assert len(d.thin) == 1
        assert "single entry" in d.thin[0].detail

    def test_a_failed_port_is_not_a_thin_pass(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["report"])
        ledger.set_step(
            "report",
            node="report",
            status=StepStatus.failed,
            outcome=StepOutcome.failed_validation,
        )
        _gate_report(
            run_dir,
            "report/main",
            [
                {
                    "port": "doc",
                    "ok": False,
                    "problems": ["min_length 20000 not met (got 900)"],
                    "measures": {"chars": 900, "min_length": 20_000},
                }
            ],
            ok=False,
        )
        assert diagnose(run_dir).thin == []


class TestRobustness:
    def test_a_truncated_last_event_line_does_not_break_the_report(
        self, tmp_path: Path
    ) -> None:
        """Telemetry must never decide whether the work can be explained."""
        run_dir = tmp_path / "run"
        ledger = _ledger(run_dir, ["a"])
        ledger.set_step(
            "a",
            node="a",
            status=StepStatus.done,
            outcome=StepOutcome.ok,
            usage=Usage(cost_usd=0.1, calls=1),
        )
        (run_dir / "events.jsonl").write_text(
            json.dumps(
                {
                    "seq": 1,
                    "ts": "T1",
                    "type": "usage",
                    "step_id": "a",
                    "payload": {"call": 1, "cost_usd": 0.1},
                }
            )
            + '\n{"seq": 2, "ts": "T2", "type": "usa',
            encoding="utf-8",
        )
        d = diagnose(run_dir)
        assert d.total.cost_usd == pytest.approx(0.1)

    def test_a_run_with_no_events_file(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _ledger(run_dir, ["a"])
        assert diagnose(run_dir).total.calls == 0

    def test_missing_state_is_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            diagnose(tmp_path / "nope")
