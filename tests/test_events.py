"""Tests for events.jsonl writer (SPEC §9)."""

from __future__ import annotations

import json
from pathlib import Path

from refract.events import EventWriter
from refract.models.ledger import Event


def _clock_seq() -> "callable":
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


class TestSeqAndTs:
    def test_seq_increments_and_ts_from_clock(self, tmp_path: Path) -> None:
        # SPEC §9
        writer = EventWriter(tmp_path, clock=_clock_seq())
        e1 = writer.emit({"type": "log"})
        e2 = writer.emit({"type": "log"})
        e3 = writer.emit({"type": "log"})

        assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)
        assert (e1.ts, e2.ts, e3.ts) == ("T1", "T2", "T3")


class TestAppendOnlyJsonl:
    def test_lines_are_newline_delimited_json_and_roundtrip(
        self, tmp_path: Path
    ) -> None:
        # SPEC §9: events.jsonl is append-only, one JSON record per line
        writer = EventWriter(tmp_path, clock=_clock_seq())
        writer.emit({"type": "run_state_changed", "payload": {"from": "created"}})
        writer.emit({"type": "node_state_changed", "step_id": "gen", "payload": {}})

        path = tmp_path / "events.jsonl"
        lines = path.read_text("utf-8").splitlines()
        assert len(lines) == 2

        records = [Event.model_validate(json.loads(line)) for line in lines]
        assert records[0].type.value == "run_state_changed"
        assert records[0].seq == 1
        assert records[1].step_id == "gen"
        assert records[1].seq == 2

    def test_new_writer_appends_and_continues_seq(self, tmp_path: Path) -> None:
        # SPEC §9/§15: a resumed run builds a second writer over the same file.
        # It must append (never truncate) AND continue the sequence — `seq` is
        # the WS replay cursor (?from_seq=), so restarting at 1 would hide every
        # post-resume event from a client that already saw the first pass.
        writer1 = EventWriter(tmp_path, clock=_clock_seq())
        writer1.emit({"type": "log"})
        writer1.emit({"type": "log"})

        writer2 = EventWriter(tmp_path, clock=_clock_seq())
        third = writer2.emit({"type": "log"})

        assert third.seq == 3
        path = tmp_path / "events.jsonl"
        lines = path.read_text("utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]

    def test_seq_recovery_ignores_a_truncated_tail(self, tmp_path: Path) -> None:
        # A crash mid-append can leave a partial last line; recovery must skip
        # it rather than blow up, and continue from the last intact record.
        path = tmp_path / "events.jsonl"
        writer1 = EventWriter(tmp_path, clock=_clock_seq())
        writer1.emit({"type": "log"})
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "ts": "T2", "ty')

        writer2 = EventWriter(tmp_path, clock=_clock_seq())
        assert writer2.emit({"type": "log"}).seq == 2


class TestDefaults:
    def test_payload_defaults_to_empty_dict_and_step_id_to_none(
        self, tmp_path: Path
    ) -> None:
        # SPEC §9
        writer = EventWriter(tmp_path, clock=_clock_seq())
        record = writer.emit({"type": "heartbeat"})

        assert record.payload == {}
        assert record.step_id is None


def test_an_unknown_event_type_is_logged_not_raised(tmp_path: Path) -> None:
    """Telemetry must never fail work that already passed its gate (SPEC §9).

    A live ``find`` step gathered 19 sources and passed its gate, then died on the
    adapter's own warning: ``'warning' is not a valid EventType``.
    """
    from refract.events import EventWriter
    from refract.models.ledger import EventType

    writer = EventWriter(tmp_path, clock=lambda: "T0")
    record = writer.emit(
        {"type": "warning", "step_id": "find", "payload": {"message": "server down"}}
    )
    assert record.type is EventType.log
    assert record.payload["unknown_event_type"] == "warning"
    assert record.payload["message"] == "server down"  # nothing is dropped
    assert record.step_id == "find"

    # a known type still round-trips unchanged
    assert writer.emit({"type": "heartbeat"}).type is EventType.heartbeat
