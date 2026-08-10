"""Tests for the Phase 2 REST/WS API (SPEC §15).

All tests use ``fastapi.testclient.TestClient`` + a MockRuntime factory — no
network, no real CLI. A temp ``projects_root`` holds a copy of
``examples/demo-project``; ``AppConfig`` points at the repo ``library`` and a
single ``claude`` provider (matching the demo project's default model
``claude/sonnet`` and how ``test_cli`` builds AppConfig). The MockRuntime writes
a valid ``requirements.md`` for ``write:*`` so the run completes fast and
deterministically.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")  # API tests need the optional `api` extra installed
from fastapi.testclient import TestClient  # noqa: E402

from refract.api import create_app  # noqa: E402
from refract.cli import AppConfig  # noqa: E402
from refract.models.config import ProvidersFile  # noqa: E402
from refract.models.pipeline import Pipeline  # noqa: E402
from refract.runtime.mock import MockRuntime, ScriptedResponse  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = REPO_ROOT / "library"
DEMO_PROJECT = REPO_ROOT / "examples" / "demo-project"

REQ = "# Requirements: Demo\n\n- FR-1: the system shall do a thing.\n"


def _clock_seq() -> Callable[[], str]:
    counter = {"n": 0}

    def clock() -> str:
        counter["n"] += 1
        return f"T{counter['n']}"

    return clock


def _app_config() -> AppConfig:
    providers = ProvidersFile.model_validate(
        {
            "providers": {
                "claude": {"api_key_env": "ANTHROPIC_API_KEY", "max_concurrent": 4}
            }
        }
    )
    return AppConfig(library_path=LIBRARY_PATH, providers=providers)


def _mock_factory(app: AppConfig, pipeline: Pipeline) -> MockRuntime:
    return MockRuntime({"write:*": [ScriptedResponse(files={"requirements.md": REQ})]})


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # keep the user's real ~/.refract out of it: user templates are written there
    monkeypatch.setenv("REFRACT_HOME", str(tmp_path / "refract-home"))
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    shutil.copytree(
        DEMO_PROJECT,
        projects_root / "demo-project",
        ignore=shutil.ignore_patterns("runs"),
    )
    api = create_app(
        projects_root=projects_root,
        app_config=_app_config(),
        runtime_factory=_mock_factory,
        clock=_clock_seq(),
    )
    return TestClient(api)


def _wait_status(
    client: TestClient,
    run_id: str,
    wanted: set[str],
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200 and resp.json()["status"] in wanted:
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {wanted} within {timeout}s")


def _wait_completed(client: TestClient, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200 and resp.json()["status"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# --- 1. list projects ---------------------------------------------------------


def test_list_projects(client: TestClient) -> None:
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert "demo-project" in resp.json()


# --- 2. pipelines + validate --------------------------------------------------


def test_pipelines_and_validate(client: TestClient) -> None:
    resp = client.get("/api/projects/demo-project/pipelines")
    assert resp.status_code == 200
    assert resp.json() == ["demo"]

    resp = client.get("/api/projects/demo-project/pipelines/demo")
    assert resp.status_code == 200
    assert "nodes:" in resp.json()["yaml"]

    resp = client.post("/api/projects/demo-project/pipelines/demo/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["errors"] == []


# --- 3. start a run in the background -> 202 {run_id}, poll to completed -------


def test_start_run_and_poll(client: TestClient) -> None:
    resp = client.post("/api/projects/demo-project/runs", json={"pipeline": "demo"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert run_id

    state = _wait_completed(client, run_id)
    assert state["status"] == "completed"
    assert state["nodes"]["scan"]["status"] == "done"
    assert state["nodes"]["write"]["status"] == "done"


# --- 4. artifacts list + fetch ------------------------------------------------


def test_artifacts_list_and_fetch(client: TestClient) -> None:
    resp = client.post("/api/projects/demo-project/runs", json={"pipeline": "demo"})
    run_id = resp.json()["run_id"]
    _wait_completed(client, run_id)

    resp = client.get(f"/api/runs/{run_id}/steps/write/artifacts")
    assert resp.status_code == 200
    files = resp.json()
    reqs = [f for f in files if f.endswith("requirements.md")]
    assert reqs, files

    resp = client.get(f"/api/runs/{run_id}/steps/write/artifacts/{reqs[0]}")
    assert resp.status_code == 200
    assert resp.text.replace("\r\n", "\n") == REQ


# --- 5. WS events replay ------------------------------------------------------


def test_ws_events_replay(client: TestClient) -> None:
    resp = client.post("/api/projects/demo-project/runs", json={"pipeline": "demo"})
    run_id = resp.json()["run_id"]
    _wait_completed(client, run_id)

    events: list[dict] = []
    with client.websocket_connect(f"/api/runs/{run_id}/events?from_seq=0") as ws:
        try:
            while True:
                events.append(ws.receive_json())
        except Exception:
            pass

    assert events
    types = {e["type"] for e in events}
    assert "run_state_changed" in types
    completed = [
        e
        for e in events
        if e["type"] == "run_state_changed"
        and e.get("payload", {}).get("to") == "completed"
    ]
    assert completed, events


# --- 6. PUT pipeline (no active run) + models --------------------------------


def test_put_pipeline_and_models(client: TestClient) -> None:
    original = client.get("/api/projects/demo-project/pipelines/demo").json()["yaml"]
    resp = client.put(
        "/api/projects/demo-project/pipelines/demo",
        content=original,
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/models")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "claude" in names
    claude = next(p for p in resp.json() if p["name"] == "claude")
    assert claude["available"] is True
    # never echo secret values, only the env var name
    assert claude["api_key_env"] == "ANTHROPIC_API_KEY"
    assert "models" in claude  # the UI builds "provider/model-id" from these


# --- pipeline write: verify, then commit (SPEC §19.2/§19.3) -------------------

_INVALID = """version: "0.1"
name: demo
nodes:
  - id: write
    type: agent
    agent: no_such_agent@1
    inputs: { sources: nowhere.sources }
"""


def test_put_rejects_invalid_pipeline_without_writing(client: TestClient) -> None:
    before = client.get("/api/projects/demo-project/pipelines/demo").json()["yaml"]

    resp = client.put(
        "/api/projects/demo-project/pipelines/demo",
        content=_INVALID,
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    codes = {e["code"] for e in detail["errors"]}
    assert "E_UNKNOWN_AGENT" in codes
    # nothing was written — the editor still sees the old pipeline
    after = client.get("/api/projects/demo-project/pipelines/demo").json()["yaml"]
    assert after == before


def test_put_allow_invalid_saves_the_draft_and_still_reports(
    client: TestClient,
) -> None:
    resp = client.put(
        "/api/projects/demo-project/pipelines/demo?allow_invalid=true",
        content=_INVALID,
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is True
    assert {e["code"] for e in body["errors"]}  # the report comes back anyway
    assert client.get("/api/projects/demo-project/pipelines/demo").json()["yaml"] == (
        _INVALID
    )


def test_put_returns_fresh_hash_matching_get(client: TestClient) -> None:
    original = client.get("/api/projects/demo-project/pipelines/demo").json()
    resp = client.put(
        "/api/projects/demo-project/pipelines/demo",
        content=original["yaml"],
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 200
    assert resp.json()["hash"] == original["hash"]
    assert (
        client.get("/api/projects/demo-project/pipelines/demo").json()["hash"]
        == (resp.json()["hash"])
    )


def test_put_with_stale_base_hash_is_refused(client: TestClient) -> None:
    original = client.get("/api/projects/demo-project/pipelines/demo").json()["yaml"]

    resp = client.put(
        "/api/projects/demo-project/pipelines/demo?base_hash=" + "0" * 64,
        content=original,
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 409
    assert "stale" in str(resp.json()["detail"])


def test_put_with_current_base_hash_succeeds(client: TestClient) -> None:
    current = client.get("/api/projects/demo-project/pipelines/demo").json()

    resp = client.put(
        f"/api/projects/demo-project/pipelines/demo?base_hash={current['hash']}",
        content=current["yaml"] + "\n# edited\n",
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 200
    assert resp.json()["committed"] is True


def test_put_leaves_no_tmp_file_behind(client: TestClient, tmp_path: Path) -> None:
    current = client.get("/api/projects/demo-project/pipelines/demo").json()
    client.put(
        "/api/projects/demo-project/pipelines/demo",
        content=current["yaml"],
        headers={"Content-Type": "text/plain"},
    )
    # the atomic write goes through <name>.yaml.tmp + os.replace
    pipelines = list((tmp_path / "projects").glob("demo-project/pipelines/*"))
    assert [p.name for p in pipelines] == ["demo.yaml"]  # the real file is there
    assert not [p for p in pipelines if p.suffix == ".tmp"]


def test_catalog_endpoint_serves_the_authoring_catalog(client: TestClient) -> None:
    resp = client.get("/api/catalog")

    assert resp.status_code == 200
    catalog = resp.json()
    assert {a["ref"] for a in catalog["agents"]} >= {"source_processor@1"}
    assert {b["type"] for b in catalog["builtins"]} >= {
        "builtin/scanner",
        "builtin/brief",
    }
    assert any(c["code"] == "E_NESTED_MAP" for c in catalog["constraints"])


# --- projects from templates + template gallery (SPEC-UI §5/§6) ---------------


def test_create_project_from_template_with_external_input(
    client: TestClient, tmp_path: Path
) -> None:
    docs = tmp_path / "client-docs"
    docs.mkdir()

    resp = client.post(
        "/api/projects",
        json={
            "name": "atlas",
            "template": "extract",
            "input": str(docs),
            "model": "claude/sonnet",
        },
    )

    assert resp.status_code == 201
    config = yaml.safe_load(
        (tmp_path / "projects" / "atlas" / "project.yaml").read_text("utf-8")
    )
    # the documents folder is referenced as given, never copied (SPEC-UI §2)
    assert config["input"] == str(docs)
    assert config["defaults"]["model"] == "claude/sonnet"
    assert not (tmp_path / "projects" / "atlas" / "input").exists()
    assert client.get("/api/projects/atlas/pipelines").json() == ["extract"]


def test_create_project_without_template_gets_its_own_input_dir(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post("/api/projects", json={"name": "blank"})

    assert resp.status_code == 201
    config = yaml.safe_load(
        (tmp_path / "projects" / "blank" / "project.yaml").read_text("utf-8")
    )
    assert config["input"] == "./input"
    assert (tmp_path / "projects" / "blank" / "input").is_dir()
    assert client.get("/api/projects/blank/pipelines").json() == []


def test_create_project_rejects_unknown_template_and_bad_names(
    client: TestClient,
) -> None:
    bad = client.post("/api/projects", json={"name": "x", "template": "nope"})
    assert bad.status_code == 400
    assert "extract" in bad.json()["detail"]  # tells you what IS available

    assert client.post("/api/projects", json={"name": "../escape"}).status_code == 400
    assert (
        client.post("/api/projects", json={"name": "demo-project"}).status_code == 409
    )


def test_template_gallery_carries_derived_metadata(client: TestClient) -> None:
    entries = {t["name"]: t for t in client.get("/api/templates").json()}

    assert {"extract", "discovery", "solution_design"} <= set(entries)
    extract = entries["extract"]
    assert extract["source"] == "library"
    assert extract["description"].startswith("Extract mode")
    assert "source_processor@1" in extract["agents"]
    assert "mcp:tavily-remote" in extract["needs"]  # UI can warn before running
    assert extract["reads_input_folder"] is True
    assert {"id": "scan", "type": "builtin/scanner"} in extract["nodes"]


def test_save_project_pipeline_as_user_template(client: TestClient) -> None:
    resp = client.post(
        "/api/templates",
        json={"name": "my-demo", "from_project": "demo-project", "pipeline": "demo"},
    )

    assert resp.status_code == 201
    entries = {t["name"]: t for t in client.get("/api/templates").json()}
    assert entries["my-demo"]["source"] == "user"
    # the gallery titles a template by the pipeline's own `name` field, so saving must
    # rename it: otherwise "my-demo" appeared in the gallery as "demo"
    assert entries["my-demo"]["title"] == "my-demo"
    # and it is immediately usable as a project template
    assert (
        client.post(
            "/api/projects", json={"name": "from-mine", "template": "my-demo"}
        ).status_code
        == 201
    )


def test_saving_a_template_refuses_to_shadow_or_duplicate(client: TestClient) -> None:
    shadow = client.post(
        "/api/templates",
        json={"name": "extract", "from_project": "demo-project", "pipeline": "demo"},
    )
    assert shadow.status_code == 409

    missing = client.post(
        "/api/templates",
        json={"name": "ok", "from_project": "demo-project", "pipeline": "nope"},
    )
    assert missing.status_code == 404


def test_project_runs_list_reports_ledger_status(client: TestClient) -> None:
    assert client.get("/api/projects/demo-project/runs").json() == []

    started = client.post("/api/projects/demo-project/runs", json={"pipeline": "demo"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    _wait_completed(client, run_id)

    runs = client.get("/api/projects/demo-project/runs").json()
    assert [r["run_id"] for r in runs] == [run_id]
    assert runs[0]["pipeline"] == "demo"
    assert runs[0]["status"] in {"completed", "failed"}


# --- serve wiring (SPEC §15 / SPEC-UI §2) --------------------------------------


def test_serve_builds_the_api_over_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `refract serve` must expose the same API the tests drive; uvicorn itself is
    # injected so no socket is opened here.
    from refract.cli import serve_impl, workspace_dir

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("REFRACT_HOME", str(tmp_path / "home"))
    workspace = workspace_dir()
    shutil.copytree(
        DEMO_PROJECT, workspace / "demo-project", ignore=shutil.ignore_patterns("runs")
    )
    seen: dict[str, object] = {}

    def runner(api: object, host: str, port: int) -> None:
        seen.update(host=host, port=port)
        with TestClient(api) as client:  # type: ignore[arg-type]
            seen["projects"] = client.get("/api/projects").json()
            seen["catalog_ok"] = client.get("/api/catalog").status_code == 200

    code = serve_impl(_app_config(), projects_root=workspace, port=1234, runner=runner)

    assert code == 0
    assert seen["host"] == "127.0.0.1"  # local tool, not exposed (I8)
    assert seen["port"] == 1234
    assert seen["projects"] == ["demo-project"]
    assert seen["catalog_ok"] is True


def test_workspace_defaults_under_refract_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from refract.cli import workspace_dir

    monkeypatch.delenv("REFRACT_WORKSPACE", raising=False)
    monkeypatch.setenv("REFRACT_HOME", str(tmp_path / "home"))

    created = workspace_dir()

    assert created == tmp_path / "home" / "projects"
    assert created.is_dir()  # created on demand, first run just works


def test_pipeline_graph_is_derived_by_the_engine(client: TestClient) -> None:
    # SPEC-UI §4: the UI draws from this, so it never parses YAML itself.
    resp = client.get("/api/projects/demo-project/pipelines/demo/graph")

    assert resp.status_code == 200
    graph = resp.json()
    assert graph["input_mode"] == "documents"
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["scan"]["type"] == "builtin/scanner"
    assert nodes["write"]["agents"] == ["demo_writer@1"]
    assert nodes["write"]["fan_out"] == "map"
    assert {"source": "scan", "target": "write", "port": "sources"} in graph["edges"]
    assert graph["order"] == ["scan", "write"]


def test_pipeline_graph_404s_for_an_unknown_pipeline(client: TestClient) -> None:
    assert (
        client.get("/api/projects/demo-project/pipelines/nope/graph").status_code == 404
    )


# --- project input: documents copied in, or a brief (SPEC-UI §4/§6) ------------


def test_documents_are_copied_into_the_project(
    client: TestClient, tmp_path: Path
) -> None:
    source = tmp_path / "client-docs"
    source.mkdir()
    (source / "transcript.md").write_text("# Call\n", encoding="utf-8")
    (source / ".DS_Store").write_text("junk", encoding="utf-8")
    (source / "attachments").mkdir()
    (source / "attachments" / "spec.md").write_text("# Spec\n", encoding="utf-8")

    resp = client.post(
        "/api/projects/demo-project/input/documents", json={"path": str(source)}
    )

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert "transcript.md" in entries
    assert "attachments" in entries  # a subfolder is one source (§13)
    assert ".DS_Store" not in entries  # tooling artifacts never become sources
    # copied, not referenced: the project keeps its own input dir
    assert resp.json()["external"] is False


def test_import_replace_clears_the_input_first(
    client: TestClient, tmp_path: Path
) -> None:
    first = tmp_path / "a"
    first.mkdir()
    (first / "old.md").write_text("old", encoding="utf-8")
    second = tmp_path / "b"
    second.mkdir()
    (second / "new.md").write_text("new", encoding="utf-8")

    client.post("/api/projects/demo-project/input/documents", json={"path": str(first)})
    resp = client.post(
        "/api/projects/demo-project/input/documents",
        json={"path": str(second), "replace": True},
    )

    entries = resp.json()["entries"]
    assert "new.md" in entries
    assert "old.md" not in entries


def test_import_rejects_a_path_that_is_not_a_folder(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/demo-project/input/documents", json={"path": "nope"}
    )
    assert resp.status_code == 400


def test_brief_is_stored_as_the_projects_source_document(client: TestClient) -> None:
    # SPEC §20.4: a research brief needs no special path — it is a document that
    # builtin/brief reads.
    resp = client.put(
        "/api/projects/demo-project/input/brief",
        json={"text": "How do warehouses handle offline receiving?"},
    )

    assert resp.status_code == 200
    assert "brief.md" in resp.json()["entries"]

    empty = client.put("/api/projects/demo-project/input/brief", json={"text": "   "})
    assert empty.status_code == 400


# --- checkpoints over the API (SPEC §21, the UI's exact calls) ------------------


def test_run_parks_at_a_checkpoint_and_continues_on_answer(
    client: TestClient,
) -> None:
    started = client.post(
        "/api/projects/demo-project/runs",
        json={"pipeline": "demo", "stop_after": ["scan"]},
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    parked = _wait_status(client, run_id, {"waiting_human"})
    assert parked["status"] == "waiting_human"
    assert parked["awaiting_checkpoint"] == "scan"
    assert parked["nodes"]["write"]["status"] == "pending"

    # what the UI shows in the banner: the reviewable outputs of that node
    files = client.get(f"/api/runs/{run_id}/steps/scan/artifacts").json()
    assert files

    assert (
        client.post(
            f"/api/runs/{run_id}/answers",
            json={"step_id": "scan", "answer": "continue"},
        ).status_code
        == 200
    )
    finished = _wait_completed(client, run_id)
    assert finished["status"] == "completed"
    assert finished["awaiting_checkpoint"] is None
    assert finished["nodes"]["write"]["status"] == "done"


def test_pipeline_graph_reports_declared_checkpoints(
    client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "projects" / "demo-project" / "pipelines" / "demo.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["checkpoints"] = ["scan"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    graph = client.get("/api/projects/demo-project/pipelines/demo/graph").json()

    assert graph["checkpoints"] == ["scan"]


def test_project_runs_report_where_a_run_is_parked(client: TestClient) -> None:
    # Reopening a project later must show that its pipeline stopped at a checkpoint
    # and where — without opening the run (SPEC §21.3).
    started = client.post(
        "/api/projects/demo-project/runs",
        json={"pipeline": "demo", "stop_after": ["scan"]},
    )
    run_id = started.json()["run_id"]
    _wait_status(client, run_id, {"waiting_human"})

    runs = client.get("/api/projects/demo-project/runs").json()

    assert runs[0]["run_id"] == run_id
    assert runs[0]["status"] == "waiting_human"
    assert runs[0]["awaiting_checkpoint"] == "scan"


def test_a_parked_runs_history_replays_over_the_socket(client: TestClient) -> None:
    # The pipeline log is events.jsonl (SPEC §9): a client that connects later must
    # get the whole history, not just what happens from now on.
    started = client.post(
        "/api/projects/demo-project/runs",
        json={"pipeline": "demo", "stop_after": ["scan"]},
    )
    run_id = started.json()["run_id"]
    _wait_status(client, run_id, {"waiting_human"})

    received = []
    with client.websocket_connect(f"/api/runs/{run_id}/events?from_seq=1") as ws:
        try:
            while True:
                received.append(ws.receive_json())
        except Exception:  # noqa: BLE001 — the server closes when the run is parked
            pass

    kinds = {e["type"] for e in received}
    assert "step_state_changed" in kinds
    assert "question" in kinds  # the checkpoint asked for a human
    assert [e["seq"] for e in received] == sorted(e["seq"] for e in received)


def test_artifacts_of_map_elements_and_loop_rounds_are_reachable(
    client: TestClient,
) -> None:
    # The API used to build the path from the step id directly, so composite ids
    # ("extract:slug", "refine.body:r1") 404'd — exactly the outputs a reviewer wants
    # to open. step_workdir() in the engine is now the single source of that naming.
    started = client.post("/api/projects/demo-project/runs", json={"pipeline": "demo"})
    run_id = started.json()["run_id"]
    state = _wait_completed(client, run_id)

    element_steps = [sid for sid in state["steps"] if sid.startswith("write:")]
    assert element_steps, "the demo map node should have element steps"
    for step_id in element_steps:
        resp = client.get(f"/api/runs/{run_id}/steps/{step_id}/artifacts")
        assert resp.status_code == 200, step_id
        assert resp.json()

    # a node id still resolves to its assembled output
    node = client.get(f"/api/runs/{run_id}/steps/write/artifacts")
    assert node.status_code == 200


def test_graph_nodes_carry_effective_models_and_checkpoints(
    client: TestClient, tmp_path: Path
) -> None:
    # The UI badges the model that will actually run a node, so the graph must carry
    # the EFFECTIVE model (resolved like the snapshot does, §7), not just what the
    # YAML happens to spell out.
    path = tmp_path / "projects" / "demo-project" / "pipelines" / "demo.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["checkpoints"] = ["scan"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    graph = client.get("/api/projects/demo-project/pipelines/demo/graph").json()
    nodes = {n["id"]: n for n in graph["nodes"]}

    assert nodes["write"]["models"] == ["claude/sonnet"]  # from project defaults
    assert nodes["scan"]["models"] == []  # a builtin runs no model
    assert nodes["scan"]["checkpoint"] is True
    assert nodes["write"]["checkpoint"] is False


def test_graph_exposes_meta_node_internals(client: TestClient, tmp_path: Path) -> None:
    # A loop IS a body and a critic, and a select IS a selector choosing between the
    # candidates' models — the UI draws them as containers, so the graph has to say
    # what is inside (SPEC-UI §4).
    project = tmp_path / "projects" / "sd"
    (project / "pipelines").mkdir(parents=True)
    (project / "input").mkdir()
    (project / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "0.1",
                "name": "sd",
                "input": "./input",
                "defaults": {"model": "claude/sonnet"},
            }
        ),
        encoding="utf-8",
    )
    shutil.copyfile(
        REPO_ROOT / "library" / "templates" / "solution_design.yaml",
        project / "pipelines" / "sd.yaml",
    )

    graph = client.get("/api/projects/sd/pipelines/sd/graph").json()
    nodes = {n["id"]: n for n in graph["nodes"]}

    refine = nodes["refine"]
    assert [b["role"] for b in refine["blocks"]] == ["body", "critic"]
    assert refine["blocks"][0]["agent"] == "requirements_writer@1"
    assert refine["blocks"][0]["model"] == "claude/sonnet"  # inherited default
    assert refine["facts"]["rounds"] == "≤3"

    choose = nodes["choose"]
    assert [b["role"] for b in choose["blocks"]] == ["selector"]
    # what the select is choosing between: the candidate producer's map_over models
    assert choose["candidate_models"] == ["claude/sonnet", "claude/opus"]
    assert choose["facts"]["fallback"] == "first_ok"

    # a winner_model binding stays a binding, not a fake provider
    sd_refine = nodes["sd_refine"]
    assert sd_refine["blocks"][0]["model"] == "@choose.winner_model"


# --- inspector edits (SPEC §19.2.1) -------------------------------------------


def test_patch_node_sets_a_model_and_keeps_the_file_valid(client: TestClient) -> None:
    before = client.get("/api/projects/demo-project/pipelines/demo").json()

    resp = client.patch(
        f"/api/projects/demo-project/pipelines/demo/nodes/write?base_hash={before['hash']}",
        json={"model": "claude/sonnet"},
    )

    assert resp.status_code == 200
    assert resp.json()["committed"] is True
    graph = client.get("/api/projects/demo-project/pipelines/demo/graph").json()
    write = next(n for n in graph["nodes"] if n["id"] == "write")
    assert write["models"] == ["claude/sonnet"]
    # the response's hash is what the client should send next
    after = client.get("/api/projects/demo-project/pipelines/demo").json()
    assert after["hash"] == resp.json()["hash"]


def test_patch_node_refuses_an_edit_that_breaks_validation(client: TestClient) -> None:
    resp = client.patch(
        "/api/projects/demo-project/pipelines/demo/nodes/write",
        json={"model": "nosuchprovider/x"},
    )

    assert resp.status_code == 409
    codes = {e["code"] for e in resp.json()["detail"]["errors"]}
    assert "E_PROVIDER_UNAVAILABLE" in codes
    # and nothing was written
    graph = client.get("/api/projects/demo-project/pipelines/demo/graph").json()
    write = next(n for n in graph["nodes"] if n["id"] == "write")
    assert write["models"] != ["nosuchprovider/x"]


def test_patch_node_rejects_unknown_node_and_bad_params(client: TestClient) -> None:
    assert (
        client.patch(
            "/api/projects/demo-project/pipelines/demo/nodes/nope",
            json={"model": "claude/sonnet"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/api/projects/demo-project/pipelines/demo/nodes/write",
            json={"params": {"max_rounds": 3}},  # not an agent-node param
        ).status_code
        == 422
    )


def test_patch_node_refuses_while_a_run_is_active(
    client: TestClient, tmp_path: Path
) -> None:
    lock_dir = tmp_path / "projects" / "demo-project" / "runs" / "run_live"
    lock_dir.mkdir(parents=True)
    (lock_dir / ".active.lock").write_text(str(os.getpid()), encoding="utf-8")

    resp = client.patch(
        "/api/projects/demo-project/pipelines/demo/nodes/write",
        json={"model": "claude/sonnet"},
    )

    assert resp.status_code == 409
