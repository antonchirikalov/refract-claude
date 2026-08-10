"""events.jsonl append-only writer (SPEC §9).

A single writer assigns the monotonic ``seq`` and timestamp, then appends one
JSON record per event. The scheduler drives an asyncio loop but emits events
synchronously from step callbacks, so a synchronous append writer (one owner,
never shared) already satisfies the "single writer" rule. The clock is
injectable so tests stay deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from refract.models.ledger import Event, EventType

EVENTS_FILENAME = "events.jsonl"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_seq(path: Path) -> int:
    """Highest ``seq`` already in ``events.jsonl`` (0 if absent/empty).

    A resumed run appends to the existing file, so the writer must continue the
    sequence instead of restarting at 1: ``seq`` is the WS replay cursor
    (``?from_seq=``, SPEC §9/§15), and duplicates would hide every post-resume
    event from a client that already saw the first pass.
    """
    if not path.exists():
        return 0
    last = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                seq = int(json.loads(line).get("seq", 0))
            except (ValueError, AttributeError):  # truncated tail after a crash
                continue
            last = max(last, seq)
    return last


class EventWriter:
    """Owns a run's ``events.jsonl``; the only writer of it (SPEC §9)."""

    def __init__(
        self, run_dir: Path | str, *, clock: Callable[[], str] = utcnow_iso
    ) -> None:
        self.path = Path(run_dir) / EVENTS_FILENAME
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = _last_seq(self.path)  # resume continues the sequence

    def emit(self, event: Mapping[str, object]) -> Event:
        """Assign ``seq``/``ts`` and append the record (append-only, UTF-8).

        An event type outside ``EventType`` (SPEC §9) is recorded as ``log`` with the
        offending name kept in the payload, rather than raised. Telemetry must not
        decide whether work stands: a live ``find`` step gathered 19 sources, passed
        its gate, and was then failed by an adapter warning emitted with a type the
        enum did not list. The record still shows what happened and to whom.
        """
        self._seq += 1
        raw_payload = event.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        raw_step = event.get("step_id")
        name = str(event["type"])
        try:
            etype = EventType(name)
        except ValueError:
            etype = EventType.log
            payload["unknown_event_type"] = name
        record = Event(
            seq=self._seq,
            ts=self._clock(),
            type=etype,
            step_id=str(raw_step) if raw_step is not None else None,
            payload=payload,
        )
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return record
