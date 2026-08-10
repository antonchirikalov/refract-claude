# refract — declarative agent pipeline engine

Engine that executes declarative pipelines (`pipeline.yaml`) of LLM agents with typed
artifact contracts. **SPEC.md is the single source of truth.** When this file and SPEC.md
disagree, SPEC.md wins. When code and SPEC.md disagree, that's a bug — fix one of them
explicitly, never silently.

## Where the internal documents live

The repo is PUBLIC; the specs, the design write-ups, the roadmap and the progress log are not.
All of them sit under `docs/`, which `.gitignore` excludes entirely. README's illustrations are
the only images that ship, and they live in `assets/`.

| Path | What |
|---|---|
| `docs/spec/SPEC.md` | engine semantics: file formats, execution, ledger, gates, runtime, phases |
| `docs/spec/SPEC-DSL.md` | normative definition of the `pipeline.yaml` language: syntax, references, types, ALL validation codes |
| `docs/spec/SPEC-UI.md` | web UI specification |
| `docs/ARCHITECTURE.md` | engine design write-up on top of the spec |
| `docs/PROGRESS.md` | current phase, live-run history, defect log |
| `docs/pipeline-engine-design-v01.md` | original engine design document |
| `docs/pipeline-project-roadmap.md` | roadmap |

Consequences to respect:

- every `SPEC §N` reference in code, tests and agent prompts means `docs/spec/SPEC.md` §N —
  the section numbering did not change, only the file location;
- README and other published documents must NOT link to these files: a clone does not have
  them and the link would 404. Refer to them as "внутренняя спецификация" instead;
- `tests/test_dsl_spec.py` reads `docs/spec/SPEC-DSL.md` and SKIPS when it is absent, so a
  checkout without the internal documents still has a green suite;
- when any of these documents changes, it changes locally only — there is nothing to push, so
  the commit message has to carry what the document now says.

## Commands

```bash
uv sync                          # install deps
uv run pytest                    # all tests (fast, no network, no real LLMs)
uv run pytest tests/test_loop.py -x -k revise   # single test
uv run ruff check --fix . && uv run ruff format .
uv run mypy refract              # strict mode, must pass
uv run refract validate examples/demo-project      # smoke check
```

Definition of done for ANY change: pytest green + mypy green + ruff clean + no invariant
violations (see below).

## Architecture map

- `refract/models/` — pydantic models for every file format in SPEC §5–§9. Formats ARE the models; never parse YAML/JSON ad-hoc.
- `refract/graph.py` — load + validate pipeline (SPEC §8). All validation errors are collected, structured `{code, node_id, message}`, never raised one-by-one.
- `refract/scheduler.py` — asyncio; a node is ready when all its input sources are done/reused.
- `refract/steps.py` — the ONE step lifecycle (SPEC §10). Meta-nodes and map reuse it; do not duplicate it.
- `refract/metanodes.py` — loop/select semantics (SPEC §10).
- `refract/runtime/` — `AgentRuntime` protocol; `claude_code.py` (real: the Claude Code CLI on a subscription), `workdir_guard.py` (the `PreToolUse` hook that makes I1 mechanical), `mock.py` (tests). No opencode in this fork.
- `refract/state.py` — ledger; `refract/events.py` — events.jsonl.
- `library/` — agent packages, artifact type registry, pipeline templates (data, not code).

## Invariants (memorize; reviewer agents enforce these)

- I1 Agents see ONLY their step workdir (`input/`, `output/`). No project paths in prompts.
- I2 Terminal step dirs are immutable.
- I3 `state.json` written only by engine, only atomically (tmp + `os.replace`).
- I4 Control decisions ONLY from typed JSON artifacts (`verdict@v1`, `selection@v1`, `question@v1`). Never parse markers from free text.
- I5 Input/output instructions in prompts are GENERATED from `agent.yaml`, never hand-written in `prompt.md`.
- I6 Agents never PRODUCE collections; fan-out lives in the engine (map). Consuming a collection is fine.
- I7 CLI/UI render only `state.json` + `events.jsonl`; no separate execution state.
- I8 Secrets never enter project folders, artifacts, or prompts; runtime env is run-level: union of snapshot providers' keys + MCP tokens from used agents' needs, nothing more.
- I9 Every AGENT step persists `prompt.md`, `raw.txt`, `agent.events.jsonl` (per attempt); builtin steps keep outputs + ledger only.
- I10 Do not implement future-phase features early (SPEC §17). Current phase is tracked in `docs/PROGRESS.md`.

## Workflow rules

- Tests first for engine logic; every feature lands with tests from SPEC §18. Tests use MockRuntime only — no network, no API keys, no real `claude` CLI.
- Windows matters: UTF-8 everywhere explicitly, `pathlib` only, no POSIX-only calls (symlink must have copy fallback).
- Keep `docs/PROGRESS.md` updated: current phase, done/remaining SPEC sections.
- Any deliberate deviation from SPEC.md: update `docs/spec/SPEC.md` in the same change, mark
  with `> CHANGED (date): reason`. The spec is untracked, so it will not appear in the commit —
  the commit message must then say what the spec now says.
- Subagents: `test-engineer` when writing tests for a module or reproducing a bug; `spec-auditor` after completing each module and before declaring a phase done; `code-reviewer` before committing non-trivial diffs. This repo is git — commit per feature.

## Gotchas

- CLI processes started by the adapter must be killed in `close()` even on crash paths (try/finally).
- I1 is enforced by a generated `PreToolUse` hook, not by `cwd`: `cwd` constrains relative paths only, and a live step wrote its whole result to an absolute path outside the workdir. The hook currently matches `Write|Edit|MultiEdit|NotebookEdit` — `Bash`/`PowerShell` go around it (SPEC §12).
- Step retry counters: `gate_retries` and `infra_retries` are independent — don't merge them.
- Re-execution of a step NEVER overwrites in place: archive to `attempts/<n>/` first (SPEC §10.2).
- Loop round number is DERIVED from the ledger, never stored separately.
- Map skips failed input items but copies them into the output collection with `status: failed` (SPEC §10.3).
- The ledger has TWO levels — `nodes` and `steps`; node `done` only after its outputs are assembled (idempotent re-assembly on resume).
- `running` steps become `pending` on ledger load — this is the crash-recovery mechanism, don't "optimize" it away.
- All linking goes through the single `link_or_copy()` helper (Windows symlink fallback).
