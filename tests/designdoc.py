"""A solution design that satisfies the ``design_doc@v1`` gate (SPEC §5).

The type's rules describe a committed client-facing proposal: five numbered sections with
section 1 subdivided, a Phase 0, tables where the material is tabular, at least one figure
placeholder with a numbered caption, and none of the three things the standard forbids —
bold as emphasis, estimates, empty headings.

Kept here rather than inline for the same reason as ``reqdoc``: a test that hand-rolls a
near-miss becomes a test about its own fixture. It is also the proof that the standard is
SATISFIABLE, which from inside the engine is indistinguishable from an agent that keeps
failing.
"""

from __future__ import annotations

_BODY = """# Solution Technical Design

{title} — technical proposal

## 1. Solution Overview

### 1.1 Business Context

The laboratory books instruments by hand and keeps calibration dates in a separate sheet
nobody reconciles with the bookings, so a test can run on an instrument whose calibration
expired days earlier. This system holds one record per instrument and refuses a booking the
calibration cannot cover. What it is not: a laboratory information management system, a
results archive, or a replacement for the metrology process itself.

| # | Role | Type | System role | Key interests | Source |
|---|------|------|-------------|---------------|--------|
| 1 | Engineer | Human actor | Books an instrument, runs the test | Sees free windows without walking the site | FR-004 |
| 2 | Metrologist | Human actor | Keeps calibration valid | Is warned before an expiry, not after | FR-008 |
| 3 | Site chief | Human actor | Resolves competing claims | Knows when a booking was overridden | G-002 |
| 4 | Verification body | External system | Holds instruments while verifying | Return date is recorded | FR-009 |

### 1.2 Core Architecture

One deployable service over one relational database, with the booking rule enforced in the
same transaction that writes the booking. The layers below each establish a boundary a check
must clear before a booking exists.

| Layer | Establishes |
|-------|-------------|
| Web client | Registry, booking calendar, metrologist worklist |
| Application service | Booking rule, reminder scheduling, audit writes |
| Relational store | Instruments, bookings, calibration events, audit log |

No component holds state another cannot rebuild: the reminder schedule is derived from
calibration dates rather than stored as a queue, so losing the scheduler loses nothing.

<!-- ILLUSTRATION: system-context-overview
     Description: the web client, the application service with the booking rule inside it,
     the relational store, and the verification body as an external actor.
     Style: technical diagram, boxes and arrows, no gradients
-->
*Figure 1. System-context overview: one service enforcing the booking rule over one store.*

### 1.3 Key Innovation / Integration

The single technical bet is that the calibration expiry is a booking constraint rather than
a report. The check runs inside the booking transaction, comparing the requested end date
against the instrument's expiry, and a booking that fails it never exists. Every other
feature — the worklist, the reminders, the audit view — reads the same two fields, so there
is one place where the rule can be wrong and one place to fix it.

## 2. Technology Stack

### 2.1 Architecture Pattern

A service-per-domain split is rejected here: the booking rule spans instruments,
calibration and bookings, and distributing it would turn one transaction into a saga for no
gain at this scale. The committed pattern is a single service with domain modules over one
PostgreSQL instance.

| # | Module | Description |
|---|--------|-------------|
| 5 | Registry | Instrument records, statuses, locations |
| 6 | Booking | The rule, the calendar, conflict handling |
| 7 | Calibration | Expiry dates, verification trips, reminders |
| 8 | Audit | Append-only record of bookings and refusals |

## 3. Delivery Phasing

The arc is registry first, then the rule that depends on it, then the work the rule enables.
Each phase is bounded by a capability someone can demonstrate.

| Phase | Core Delivery | Modules | Phase Exit Criterion |
|-------|---------------|---------|----------------------|
| 0 | Discovery and architecture validation | 5, 6 | Joint sign-off on the data model and the booking rule |
| 1 | Registry and calibration dates | 5, 7 | Every instrument has a record and an expiry date |
| 2 | Booking with the rule enforced | 6, 8 | A booking past an expiry is refused and the refusal is logged |

### Phase 0 — Discovery and Architecture Validation

This phase validates the model against the three spreadsheets in use and confirms the
booking rule against the metrologist's actual statuses. Its exit state is a signed-off model
and a rule nobody disputes.

### Phase 1 — Registry and calibration dates

The phase completes the instrument registry with its statuses and expiry dates. Its exit
state is that every instrument in the laboratory has one record.

Migration scenario. An operator imports the supply spreadsheet; the importer matches rows to
existing records by inventory number, reports the rows it could not match, and writes the
rest. The metrologist then reviews the instruments whose expiry is missing.

<!-- ILLUSTRATION: phase-one-import
     Description: the spreadsheet, the importer, the matched and unmatched paths.
     Style: technical diagram, boxes and arrows, no gradients
-->
*Figure 2. Phase 1 import flow, with the unmatched rows going to review.*

| # | Module | Description |
|---|--------|-------------|
| 9 | Importer | Reads the existing sheets, matches by inventory number |

## 4. Non-Functional Requirements

| Category | Requirement | Design Approach |
|----------|-------------|-----------------|
| Performance | A booking screen renders in under 2 seconds | One indexed query per calendar month |
| Security | Runs inside the internal network | No public ingress; access through the corporate network only |
| Compliance | Refusals kept 3 years | Append-only audit table with a retention job |

## 5. Infrastructure and Deployment

This section is a first-pass approximation to be refined during discovery.

Traffic enters from the corporate network only. The service runs on two internal hosts
behind the existing reverse proxy, with state in a managed PostgreSQL instance and nightly
backups to internal storage.

<!-- ILLUSTRATION: deployment-topology
     Description: the corporate network, the reverse proxy, two service hosts, the database.
     Style: technical diagram, boxes and arrows, no gradients
-->
*Figure 3. Deployment topology inside the internal network.*

| Category | Service / Component | Usage |
|----------|---------------------|-------|
| Compute | Two internal Linux hosts | Application service |
| Database | PostgreSQL | Instruments, bookings, audit |
| Ingress | Existing reverse proxy | Internal traffic only |

## Risks and mitigations

- The three source sheets disagree about instrument counts; the import reports mismatches
  rather than guessing, and the metrologist resolves them.
- A verification trip with no return date leaves an instrument unavailable indefinitely; the
  worklist surfaces trips older than their expected duration.

## Assumptions to confirm

- Bookings are per whole day; no source states an hourly granularity.
- Reminders go out by email, the only channel currently in use.
"""


def design_doc(title: str = "Demo") -> str:
    """A minimal document that passes every ``design_doc@v1`` rule."""
    return _BODY.format(title=title)


DESIGN_DOC = design_doc()
