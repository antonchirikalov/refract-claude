"""A requirements document that satisfies the ``requirements@v1`` gate (SPEC §5).

The type's rules describe a traceable document: identifiers three digits wide, requirements
in tables with a source column, grouped by business area, and a section 8 split into
conflicts, gaps and assumptions. The ``requirements_to_design`` template adds depth floors
on top — ten requirements, four business areas, priorities that discriminate.

That is a lot to satisfy by hand in every test, and a test that hand-rolls a near-miss is a
test about its own fixture rather than about the engine. It is also the check that the
standard is SATISFIABLE: rules no document can pass are indistinguishable, from inside the
engine, from an agent that keeps failing.
"""

from __future__ import annotations

_BODY = """# Requirements: {title}

## Source index

| Source | Type | Trust |
|--------|------|-------|
| `brief.md` | brief | high |
| `interview-metrologist.md` | interview | medium |

## Domain grounding

The client runs one testing laboratory whose instruments are booked by hand and whose
calibration dates live in a separate spreadsheet nobody reconciles with the bookings. The
system replaces both with one record per instrument, so that a booking can be refused when
the calibration behind it has expired.

## 1. Stakeholders and roles

| Role | What they do |
|------|--------------|
| Engineer | Books an instrument and runs the test on it. |
| Metrologist | Keeps calibration valid and sends instruments out for verification. |
| Site chief | Resolves competing claims on the same instrument. Unconfirmed: named once in `brief.md` with no duties given. |

## 2. Business context

- Bookings are kept in a spreadsheet today and are not reconciled with calibration. `brief.md`
- Two tests had to be repeated last quarter after running on an expired instrument. `interview-metrologist.md`

## 3. Functional requirements

### 3.1 Instrument registry

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-001 | The system shall hold one record per instrument, with its location and status. | MUST | `brief.md` |
| FR-002 | The system shall record the date each instrument's calibration expires. | MUST | `brief.md` |
| FR-003 | The system shall support the six instrument statuses the metrologist uses. | MUST | `interview-metrologist.md` |

### 3.2 Booking

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-004 | The system shall record who holds an instrument and until when. | MUST | `brief.md` |
| FR-005 | The system shall refuse a booking that would run past the calibration expiry. | MUST | `brief.md` |
| FR-006 | The system shall show free windows for one instrument a month ahead. | SHOULD | `brief.md` |
| FR-007 | The system shall let an engineer search for any instrument meeting stated conditions. | COULD | `interview-metrologist.md` |

### 3.3 Calibration and reminders

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-008 | The system shall remind the metrologist 30, 14 and 3 days before an expiry. | MUST | `interview-metrologist.md` |
| FR-009 | The system shall mark an instrument unavailable while it is away for verification. | MUST | `interview-metrologist.md` |
| FR-010 | The system shall reset the calibration term after a repair. | SHOULD | `interview-metrologist.md` |

### 3.4 Audit

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-011 | The system shall show, for any past test, the instrument used and its calibration status that day. | MUST | `brief.md` |

### 3.5 Out of MVP scope

| Feature | Source |
|---------|--------|
| Native mobile application | `brief.md` ("the engineers have desktops") |

## 4. Non-functional requirements

| ID | Requirement | Category | Source |
|----|-------------|----------|--------|
| NFR-001 | A booking screen shall render in under 2 seconds. | Performance | `brief.md` |
| NFR-002 | The system shall run entirely inside the internal network. | Security | `brief.md` |
| NFR-003 | Every booking refusal shall be logged with its reason for 3 years. | Legal / Compliance | `brief.md` |

## 5. Business rules and constraints

| ID | Rule | Source |
|----|------|--------|
| BR-001 | An instrument out of calibration shall never be bookable. | `brief.md` |
| BR-002 | An instrument away for verification shall never appear as free. | `interview-metrologist.md` |

## 6. Data model

| Entity | Attributes | Notes |
|--------|------------|-------|
| Instrument | id, location, status, calibration due date | `brief.md` |
| Booking | instrument, holder, start, end | `brief.md` |

## 7. Integration points

No source names an external system this one must talk to. Recorded here so that the absence
is a finding rather than a section somebody forgot. {integrations}

## 8. Open questions, conflicts and assumptions

### 8.1 Conflicts

No source contradicts another, and none contradicts itself. Stated rather than omitted, so
that a reader can tell this was checked.

### 8.2 Gaps

| # | Gap | Impact |
|---|-----|--------|
| G-001 | The number of instruments is not stated. | Sizing, licence count |
| G-002 | Priority between competing bookings is decided verbally. | Booking rules, escalation design |

### 8.3 Assumptions

| # | Assumption | Basis |
|---|------------|-------|
| A-001 | Bookings are per whole day. | No source states an hourly granularity. |
| A-002 | Reminders go out by email. | No source names a channel; email is the only one in use. |
"""


def requirements_doc(title: str = "Demo", *, integrations: str = "") -> str:
    """A minimal document that passes ``requirements@v1`` and the template's depth floors."""
    return _BODY.format(title=title, integrations=integrations)


REQ_DOC = requirements_doc()
