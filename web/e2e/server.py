"""API + built UI for the end-to-end tests, with a scripted runtime.

Playwright boots this (see playwright.config.ts). It is the real engine and the real
API — only the AgentRuntime is scripted, the same way the python suite injects
MockRuntime (SPEC §18): no network, no provider quota, no CLI.

Each start gets a fresh workspace under a temp dir, so a spec's assertions never
depend on what a previous run left behind.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from refract.api import create_app  # noqa: E402
from refract.cli import AppConfig  # noqa: E402
from refract.models.config import McpFile, ProvidersFile  # noqa: E402
from refract.runtime.base import EventCallback, StepResult, StepSpec  # noqa: E402

# The prompt carries each output's port, type and path (generated from the contract,
# I5), which is all a scripted runtime needs to produce believable artifacts.
OUTPUT_RE = re.compile(
    r"### `(?P<port>[^`]+)` \((?P<type>[^)]+)\)(?P<opt> — optional)?\s*\n+"
    r"Write to: `(?P<path>[^`]+)`",
    re.MULTILINE,
)

REQUIREMENTS = """# Requirements: Warehouse Goods Receiving

## Functional

- FR-1: The receiver scans a pallet barcode on an Android handheld.
- FR-2: The system validates the scan against the ERP's expected delivery.

## Non-functional

- NFR-1: Receiving stays usable for 4 hours without network.
"""

DESIGN = (
    "# Solution Design\n\nAn offline-first client over an ERP integration layer.\n"
    + ("\nBody paragraph with enough substance to clear the length rule.\n" * 30)
    # design_doc@v1 requires both sections: the gate checks that they exist, the critic
    # judges whether they are honest
    + "\n## Risks and mitigations\n\n- Sync conflicts; supervisor review.\n"
    + "\n## Assumptions to confirm\n\n- Versions named above are proposals.\n"
)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_ok_slug(workdir: Path) -> str:
    for manifest in workdir.glob("input/*/_collection.json"):
        for item in json.loads(manifest.read_text("utf-8")).get("items", []):
            if item.get("status") == "ok":
                return str(item["slug"])
    return "unknown"


class ScriptedRuntime:
    """Writes contract-shaped artifacts instead of calling a model."""

    def __init__(self, step_delay_s: float = 0.35) -> None:
        self._delay = step_delay_s

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        on_event(
            {"type": "heartbeat", "step_id": spec.step_id, "payload": {"elapsed_s": 1}}
        )
        await asyncio.sleep(self._delay)  # so the UI has something to show
        for match in OUTPUT_RE.finditer(spec.prompt):
            if match.group("opt"):
                continue  # optional ports (question@v1) stay unwritten
            atype = match.group("type")
            target = spec.workdir / match.group("path").rstrip("/")
            if atype == "verdict@v1":
                _write_json(target, {"verdict": "approved", "issues": []})
            elif atype == "selection@v1":
                _write_json(
                    target,
                    {"winner": _first_ok_slug(spec.workdir), "reason": "scripted"},
                )
            elif atype == "extract@v1":
                _write_json(
                    target,
                    {
                        "source": spec.step_id.split(":")[-1],
                        "requirements": [
                            {"text": "Barcode receiving.", "category": "functional"}
                        ],
                        "decisions": [],
                        "constraints": [],
                        "open_questions": [],
                        "trust_level": "medium",
                    },
                )
            elif atype == "requirements@v1":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(REQUIREMENTS, encoding="utf-8")
            elif atype == "design_doc@v1":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(DESIGN, encoding="utf-8")
            elif atype == "found_sources@v1":
                target.mkdir(parents=True, exist_ok=True)
                (target / "source-one.md").write_text("# One\n", encoding="utf-8")
                (target / "source-two.md").write_text("# Two\n", encoding="utf-8")
                (target / "source-three.md").write_text("# Three\n", encoding="utf-8")
            elif atype in ("arch_report@v1", "discovery_report@v1", "brief@v1"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# Questions\n\n1. Which ERP?\n", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "note.md").write_text("# note\n", encoding="utf-8")
        (spec.workdir / "raw.txt").write_text("[scripted runtime]", encoding="utf-8")
        (spec.workdir / "agent.events.jsonl").write_text(
            json.dumps({"type": "log", "payload": {"message": "scripted"}}) + "\n",
            encoding="utf-8",
        )
        return StepResult(completed=True, usage={"cost": 0})

    async def close(self) -> None:
        return None


CONFIRM_PIPELINE = """version: "0.1"
name: confirm
input_mode: brief

# `source_finder` is the only agent here that needs `webfetch`, and it sits on a PLAIN
# agent node — the one shape capability confirmation is wired for (SPEC §16.10).
nodes:
  - id: brief
    type: builtin/brief

  - id: find
    type: agent
    agent: source_finder@1
    inputs: { brief: brief.brief }
"""


def _seed_confirm_project(workspace: Path) -> None:
    """A project whose policy requires approval before a network-using step runs."""
    project = workspace / "needs-approval"
    (project / "pipelines").mkdir(parents=True)
    (project / "input").mkdir()
    (project / "input" / "brief.md").write_text(
        "How do warehouses handle offline barcode receiving?\n", encoding="utf-8"
    )
    (project / "pipelines" / "confirm.yaml").write_text(
        CONFIRM_PIPELINE, encoding="utf-8"
    )
    (project / "project.yaml").write_text(
        "version: '0.1'\n"
        "name: needs-approval\n"
        "input: ./input\n"
        "defaults:\n"
        "  model: claude/sonnet\n"
        "confirm: [webfetch]\n",
        encoding="utf-8",
    )


CHAIN_PIPELINE = """version: "0.1"
name: chain

# A refine loop whose BODY IS A CHAIN (SPEC §10.3): write, then fact-check against the
# same extracts, then one critic decides. Exercises body1/body2 in the graph and the
# inspector.
nodes:
  - id: scan
    type: builtin/scanner

  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources

  - id: refine
    type: loop
    params: { max_rounds: 2 }
    body:
      - agent: requirements_writer@1
        inputs: { extracts: extract.extract }
      - agent: requirements_fact_checker@1
        inputs: { draft: "@prev", extracts: extract.extract }
    critic:
      agent: requirements_critic@1
      inputs: { draft: "@body", extracts: extract.extract }
    outputs: { doc: "@body" }
"""


def _seed_chain_project(workspace: Path) -> None:
    """A project whose loop body has two elements, for the container UI."""
    project = workspace / "chain-project"
    shutil.copytree(
        REPO / "examples" / "extract-project",
        project,
        ignore=shutil.ignore_patterns("runs", "node_modules", "pipelines"),
    )
    (project / "pipelines").mkdir(exist_ok=True)
    (project / "pipelines" / "chain.yaml").write_text(CHAIN_PIPELINE, encoding="utf-8")
    (project / "project.yaml").write_text(
        "version: '0.1'\n"
        "name: chain-project\n"
        "input: ./input\n"
        "defaults:\n"
        "  model: claude/sonnet\n",
        encoding="utf-8",
    )


def build() -> tuple[object, Path]:
    home = Path(tempfile.mkdtemp(prefix="refract-e2e-"))
    workspace = home / "projects"
    workspace.mkdir(parents=True)
    os.environ["REFRACT_HOME"] = str(home)
    os.environ.setdefault("ANTHROPIC_API_KEY", "e2e")
    # a project with documents already in place, for specs that only need to run one
    shutil.copytree(
        REPO / "examples" / "extract-project",
        workspace / "extract-project",
        ignore=shutil.ignore_patterns("runs", "node_modules"),
    )
    _seed_confirm_project(workspace)
    _seed_chain_project(workspace)
    app_config = AppConfig(
        library_path=REPO / "library",
        providers=ProvidersFile.model_validate(
            {
                "providers": {
                    "claude": {
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "models": ["sonnet", "opus"],
                    },
                }
            }
        ),
        # declared so validation passes; never launched — the runtime is scripted
        mcp=McpFile.model_validate(
            {
                "servers": {
                    name: {"command": ["true"]}
                    for name in ("tavily-remote", "pdf-reader")
                }
            }
        ),
    )
    api = create_app(
        projects_root=workspace,
        app_config=app_config,
        runtime_factory=lambda app, pipeline: ScriptedRuntime(),
        static_dir=REPO / "web" / "dist",
    )
    return api, workspace


if __name__ == "__main__":
    import uvicorn

    api, workspace = build()
    print(f"e2e workspace: {workspace}", flush=True)
    uvicorn.run(api, host="127.0.0.1", port=8799, log_level="warning")
