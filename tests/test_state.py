"""Tests for the run ledger (refract/state.py) — SPEC §9."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.models.ledger import NodeStatus, RunStatus, StepOutcome, StepStatus
from refract.state import STATE_FILENAME, Ledger, read_state, step_workdir


def _make_ledger(run_dir: Path) -> Ledger:
    return Ledger.create(
        run_dir,
        run_id="run-1",
        pipeline="demo.yaml",
        node_ids=["a", "b"],
        created_at="2026-07-20T00:00:00Z",
    )


def test_round_trip_persistence(tmp_path: Path) -> None:
    """SPEC §9: create -> mutate -> load returns an equal state."""
    ledger = _make_ledger(tmp_path)
    ledger.set_node_status("a", NodeStatus.running)
    ledger.set_step(
        "a#1",
        node="a",
        status=StepStatus.done,
        outcome=StepOutcome.ok,
        tries=1,
        started_at="2026-07-20T00:00:01Z",
        finished_at="2026-07-20T00:00:02Z",
    )
    ledger.set_node_status("a", NodeStatus.done)

    reloaded = Ledger.load(tmp_path)
    assert reloaded.state.model_dump() == ledger.state.model_dump()


def test_crash_recovery_running_to_pending(tmp_path: Path) -> None:
    """SPEC §9: running steps/nodes become pending on load; other statuses untouched."""
    ledger = _make_ledger(tmp_path)
    ledger.set_step("a#1", node="a", status=StepStatus.running, tries=1)
    ledger.set_step(
        "b#1", node="b", status=StepStatus.done, outcome=StepOutcome.ok, tries=1
    )
    ledger.set_node_status("a", NodeStatus.running)
    ledger.set_node_status("b", NodeStatus.failed, error="boom")

    reloaded = Ledger.load(tmp_path)

    assert reloaded.get_step("a#1").status is StepStatus.pending
    assert reloaded.get_step("b#1").status is StepStatus.done
    assert reloaded.get_node("a").status is NodeStatus.pending
    assert reloaded.get_node("b").status is NodeStatus.failed
    assert reloaded.get_node("b").error == "boom"

    # The recovery must have been persisted back to disk.
    on_disk = json.loads((tmp_path / STATE_FILENAME).read_text("utf-8"))
    assert on_disk["steps"]["a#1"]["status"] == "pending"
    assert on_disk["nodes"]["a"]["status"] == "pending"
    assert on_disk["steps"]["b#1"]["status"] == "done"
    assert on_disk["nodes"]["b"]["status"] == "failed"


def test_atomicity_no_tmp_left_and_valid_json(tmp_path: Path) -> None:
    """I3: save() is atomic (tmp + os.replace); no stray tmp; content always valid."""
    ledger = _make_ledger(tmp_path)
    ledger.set_node_status("a", NodeStatus.running)

    tmp_path_file = tmp_path / (STATE_FILENAME + ".tmp")
    assert not tmp_path_file.exists()

    raw = json.loads((tmp_path / STATE_FILENAME).read_text("utf-8"))
    from refract.models.ledger import RunState

    RunState.model_validate(raw)  # must round-trip without error


def test_stale_tmp_file_does_not_corrupt_load(tmp_path: Path) -> None:
    """A stray leftover .tmp file from an interrupted write must not affect load()."""
    ledger = _make_ledger(tmp_path)
    ledger.set_node_status("a", NodeStatus.done)

    stale_tmp = tmp_path / (STATE_FILENAME + ".tmp")
    stale_tmp.write_text("{not valid json", encoding="utf-8")

    reloaded = Ledger.load(tmp_path)
    assert reloaded.get_node("a").status is NodeStatus.done

    # save() again should still succeed and clean up (replace) the tmp file.
    reloaded.set_node_status("b", NodeStatus.done)
    assert not stale_tmp.exists() or json.loads(stale_tmp.read_text("utf-8"))


def test_set_step_insert_then_update(tmp_path: Path) -> None:
    """set_step inserts on first call, updates in place on subsequent calls."""
    ledger = _make_ledger(tmp_path)

    ledger.set_step("a#1", node="a", status=StepStatus.running, tries=1)
    step = ledger.get_step("a#1")
    assert step is not None
    assert step.status is StepStatus.running
    assert step.tries == 1
    assert step.outcome is None

    ledger.set_step(
        "a#1",
        node="a",
        status=StepStatus.done,
        outcome=StepOutcome.ok,
        tries=2,
        finished_at="2026-07-20T00:01:00Z",
    )
    step = ledger.get_step("a#1")
    assert step is not None
    assert step.status is StepStatus.done
    assert step.outcome is StepOutcome.ok
    assert step.tries == 2
    assert step.finished_at == "2026-07-20T00:01:00Z"

    # No duplicate record was created.
    assert len(ledger.state.steps) == 1


def test_set_node_selection_records_winner(tmp_path: Path) -> None:
    """SPEC §10.3: select node exports winner/winner_model onto the node record."""
    ledger = _make_ledger(tmp_path)
    ledger.set_node_selection("a", winner="draft-2", winner_model="gpt-x")

    node = ledger.get_node("a")
    assert node is not None
    assert node.winner == "draft-2"
    assert node.winner_model == "gpt-x"

    reloaded = Ledger.load(tmp_path)
    assert reloaded.get_node("a").winner == "draft-2"
    assert reloaded.get_node("a").winner_model == "gpt-x"


def test_reset_failed_steps_only_failed(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    ledger.set_step(
        "a#1",
        node="a",
        status=StepStatus.failed,
        outcome=StepOutcome.failed_agent,
        tries=1,
        error="agent crashed",
    )
    ledger.set_step("a#2", node="a", status=StepStatus.done, tries=1)
    ledger.set_step("b#1", node="b", status=StepStatus.running, tries=1)

    reset_ids = ledger.reset_failed_steps()

    assert reset_ids == ["a#1"]
    step = ledger.get_step("a#1")
    assert step is not None
    assert step.status is StepStatus.pending
    assert step.outcome is None
    assert step.error is None

    # Untouched steps stay as they were.
    assert ledger.get_step("a#2").status is StepStatus.done
    assert ledger.get_step("b#1").status is StepStatus.running


def test_reset_failed_steps_empty_when_none_failed(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    ledger.set_step("a#1", node="a", status=StepStatus.done, tries=1)

    assert ledger.reset_failed_steps() == []


def test_enum_values_serialize_as_strings(tmp_path: Path) -> None:
    """SPEC §9: enums must serialize to their plain string form on disk."""
    ledger = _make_ledger(tmp_path)
    ledger.set_run_status(RunStatus.running)
    ledger.set_node_status("a", NodeStatus.running)
    ledger.set_step("a#1", node="a", status=StepStatus.running, tries=1)

    raw = json.loads((tmp_path / STATE_FILENAME).read_text("utf-8"))
    assert raw["status"] == "running"
    assert raw["nodes"]["a"]["status"] == "running"
    assert raw["steps"]["a#1"]["status"] == "running"


def test_node_ids_lists_all_nodes(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    assert set(ledger.node_ids()) == {"a", "b"}


class TestStepWorkdir:
    """step_id → step directory, the one place that knows the naming (§9/§10.2)."""

    def test_plain_and_builtin_steps_live_under_main(self, tmp_path: Path) -> None:
        assert step_workdir(tmp_path, "scan") == tmp_path / "steps" / "scan" / "main"

    def test_map_element_uses_its_slug(self, tmp_path: Path) -> None:
        assert (
            step_workdir(tmp_path, "extract:rfp-excerpt-md")
            == tmp_path / "steps" / "extract" / "rfp-excerpt-md"
        )

    def test_loop_round_matches_the_engine_layout(self, tmp_path: Path) -> None:
        assert (
            step_workdir(tmp_path, "refine.body:r1")
            == tmp_path / "steps" / "refine" / "body_r1"
        )
        assert (
            step_workdir(tmp_path, "refine.critic:r2")
            == tmp_path / "steps" / "refine" / "critic_r2"
        )

    def test_selector_step(self, tmp_path: Path) -> None:
        assert (
            step_workdir(tmp_path, "choose.selector")
            == tmp_path / "steps" / "choose" / "selector"
        )


class TestConcurrentReaders:
    """A reader must not be able to break a run (Windows atomic-replace sharing)."""

    def test_save_survives_a_reader_holding_the_file(self, tmp_path: Path) -> None:
        # Found live: a UI polling state.json made os.replace raise PermissionError
        # inside the scheduler, which aborted the whole run with a node stuck at
        # `running`. The write now retries instead of exploding.
        import threading

        ledger = Ledger.create(
            tmp_path,
            run_id="r",
            pipeline="p",
            node_ids=["a"],
            created_at="T0",
        )
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    read_state(tmp_path)
                except RuntimeError:
                    pass

        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(200):
                ledger.set_node_status(
                    "a", NodeStatus.running if i % 2 else NodeStatus.done
                )
        finally:
            stop.set()
            t.join()

        assert read_state(tmp_path)["run_id"] == "r"

    def test_read_state_tolerates_a_torn_read(self, tmp_path: Path) -> None:
        Ledger.create(
            tmp_path, run_id="r", pipeline="p", node_ids=["a"], created_at="T0"
        )
        (tmp_path / STATE_FILENAME).write_text("", encoding="utf-8")  # mid-write shape

        with pytest.raises(RuntimeError, match="could not read"):
            read_state(tmp_path)


# --- usage accounting (SPEC §9) ---------------------------------------------


def test_usage_from_report_normalises_the_runtime_dialect() -> None:
    """``StepResult.usage`` is a loose dict by contract (SPEC §12)."""
    from refract.models.ledger import Usage

    assert Usage.from_report(None) is None

    empty = Usage.from_report({})
    assert empty is not None and empty.calls == 1 and empty.cost_usd == 0.0

    full = Usage.from_report(
        {
            "cost": 0.42,
            "tokens": {
                "input_tokens": 1000,
                "output_tokens": 250,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 40,
            },
            "duration_ms": 8000,
        }
    )
    assert full is not None
    assert (full.cost_usd, full.input_tokens, full.output_tokens) == (0.42, 1000, 250)
    assert (full.cache_read_tokens, full.cache_write_tokens) == (60, 40)
    assert full.duration_ms == 8000 and full.calls == 1


def test_usage_from_report_survives_garbage() -> None:
    """A runtime reporting nonsense must not take the run down with it."""
    from refract.models.ledger import Usage

    junk = Usage.from_report({"cost": "free", "tokens": "lots", "duration_ms": None})
    assert junk is not None
    assert junk.cost_usd == 0.0 and junk.input_tokens == 0 and junk.calls == 1
    # booleans are not numbers here: True must not become a cost of 1.0
    assert Usage.from_report({"cost": True}) == Usage(calls=1)


def test_total_and_per_node_usage_are_derived(tmp_path: Path) -> None:
    """Derived, not stored: a re-executed step reports its own total (SPEC §9)."""
    from refract.models.ledger import Usage

    ledger = _make_ledger(tmp_path)
    ledger.set_step(
        "a", node="a", status=StepStatus.done, usage=Usage(cost_usd=0.10, calls=1)
    )
    ledger.set_step(
        "b:1", node="b", status=StepStatus.done, usage=Usage(cost_usd=0.20, calls=2)
    )
    ledger.set_step(
        "b:2", node="b", status=StepStatus.done, usage=Usage(cost_usd=0.05, calls=1)
    )
    # a reused step spent nothing in THIS run
    ledger.set_step("b:3", node="b", status=StepStatus.reused)

    total = ledger.total_usage()
    assert total.cost_usd == pytest.approx(0.35)
    assert total.calls == 4
    by_node = ledger.usage_by_node()
    assert by_node["a"].cost_usd == pytest.approx(0.10)
    assert by_node["b"].cost_usd == pytest.approx(0.25)
    assert "b:3" not in by_node


def test_re_running_a_step_replaces_its_usage_rather_than_doubling(
    tmp_path: Path,
) -> None:
    from refract.models.ledger import Usage

    ledger = _make_ledger(tmp_path)
    ledger.set_step(
        "a", node="a", status=StepStatus.failed, usage=Usage(cost_usd=0.30, calls=3)
    )
    ledger.set_step(
        "a", node="a", status=StepStatus.done, usage=Usage(cost_usd=0.10, calls=1)
    )
    assert ledger.total_usage().cost_usd == pytest.approx(0.10)


def test_usage_survives_a_ledger_round_trip(tmp_path: Path) -> None:
    from refract.models.ledger import Usage

    ledger = _make_ledger(tmp_path)
    ledger.set_step(
        "a",
        node="a",
        status=StepStatus.done,
        usage=Usage(cost_usd=0.5, input_tokens=10, calls=1),
    )
    reloaded = Ledger.load(tmp_path)
    step = reloaded.get_step("a")
    assert step is not None and step.usage is not None
    assert step.usage.cost_usd == pytest.approx(0.5)
    assert step.usage.input_tokens == 10


def test_a_ledger_written_before_usage_existed_still_loads(tmp_path: Path) -> None:
    """Old runs have no ``usage`` key — reading them must not fail (SPEC §9)."""
    ledger = _make_ledger(tmp_path)
    ledger.set_step("a", node="a", status=StepStatus.done, outcome=StepOutcome.ok)
    raw = json.loads((tmp_path / STATE_FILENAME).read_text("utf-8"))
    del raw["steps"]["a"]["usage"]
    (tmp_path / STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")

    reloaded = Ledger.load(tmp_path)
    step = reloaded.get_step("a")
    assert step is not None and step.usage is None
    assert reloaded.total_usage() == reloaded.total_usage().__class__()
