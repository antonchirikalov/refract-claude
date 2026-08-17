"""Run snapshot builder (SPEC §9).

Execution and resume read ONLY the snapshot, never the live project/library.
A snapshot captures, under ``<run_dir>/snapshot/``:

- ``pipeline.yaml``  — verbatim copy of the source pipeline file.
- ``resolved.yaml``  — the pipeline with every node/sub-block's effective
  ``model`` and all params (defaults filled) written out.
- ``agents/<name>@<ver>/`` — FULL copies of every agent package used by the graph.
- ``agents.lock.json`` — ``{"<name>@<ver>": "sha256:..."}`` package hashes.

Package hash: sha256 of the sorted lines ``"<relpath>:<sha256(file)>"`` (SPEC §9),
so it is stable across platforms (relative POSIX paths, byte-level file digests).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from refract.models.agent import AgentSpec
from refract.models.pipeline import (
    AgentNode,
    BuiltinNode,
    DiscoverNode,
    LoopNode,
    Pipeline,
    SelectNode,
)

SNAPSHOT_DIRNAME = "snapshot"
_LOCK_FILENAME = "agents.lock.json"


# --- package hashing --------------------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def package_hash(pkg_dir: Path | str) -> str:
    """sha256 of sorted ``"<relpath>:<sha256(file)>"`` lines (SPEC §9)."""
    pkg_dir = Path(pkg_dir)
    # Sort the RENDERED lines (ASCII byte order), not the Path objects: Path
    # comparison is case-insensitive on Windows but case-sensitive on POSIX,
    # which would make the digest platform-dependent (SPEC §9 hashes sorted lines).
    lines = sorted(
        f"{p.relative_to(pkg_dir).as_posix()}:{_file_sha256(p)}"
        for p in pkg_dir.rglob("*")
        if p.is_file()
    )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# --- model resolution (SPEC §7 priority; bindings kept verbatim) ------------


def resolve_model(
    key: str,
    node_model: str | None,
    *,
    overrides: dict[str, str],
    default_model: str | None,
) -> str | None:
    """Effective model for a step key (SPEC §7).

    Priority: run override (``--model-for KEY``) → node/sub-block ``model:`` →
    project ``defaults.model``. A ``@<select>.winner_model`` binding is a scalar
    binding (§8.1) and is passed through unresolved for the scheduler to bind.
    """
    if node_model is not None and node_model.startswith("@"):
        return node_model
    return overrides.get(key) or node_model or default_model


# --- used-agent discovery ---------------------------------------------------


def node_agent_refs(pipeline: Pipeline) -> dict[str, set[str]]:
    """Which agent packages each node binds, keyed by node id.

    Needed to turn "this agent package changed" into "these nodes cannot be reused"
    (SPEC §10.5). Builtins bind none and simply get an empty set.
    """
    out: dict[str, set[str]] = {}
    for node in pipeline.nodes:
        if isinstance(node, AgentNode):
            out[node.id] = {node.agent}
        elif isinstance(node, LoopNode):
            out[node.id] = {b.agent for b in node.body_chain} | {node.critic.agent}
        elif isinstance(node, SelectNode):
            out[node.id] = {node.selector.agent}
        elif isinstance(node, DiscoverNode):
            out[node.id] = {node.agent}
        else:
            out[node.id] = set()
    return out


def used_agent_refs(pipeline: Pipeline) -> list[str]:
    """Distinct ``name@version`` refs the graph binds, in first-seen order."""
    refs: list[str] = []
    seen: set[str] = set()

    def add(ref: str) -> None:
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for node in pipeline.nodes:
        if isinstance(node, AgentNode):
            add(node.agent)
        elif isinstance(node, LoopNode):
            for block in node.body_chain:
                add(block.agent)
            add(node.critic.agent)
        elif isinstance(node, SelectNode):
            add(node.selector.agent)
        elif isinstance(node, DiscoverNode):
            add(node.agent)
    return refs


# --- resolved.yaml ----------------------------------------------------------


def _agent_timeout(ref: str, agents: dict[str, AgentSpec]) -> int:
    spec = agents.get(ref)
    return spec.defaults.timeout_s if spec is not None else 3600


def _fill_retry_params(params: dict[str, object], fallback_timeout: int) -> None:
    """Fill a resolved node/sub-block's ``timeout_s`` from the agent default."""
    if params.get("timeout_s") is None:
        params["timeout_s"] = fallback_timeout


def build_resolved(
    pipeline: Pipeline,
    *,
    agents: dict[str, AgentSpec],
    overrides: dict[str, str],
    default_model: str | None,
) -> dict[str, object]:
    """The pipeline as a plain dict with effective models + filled params (§9)."""
    data = pipeline.model_dump(mode="json")
    by_id = {n["id"]: n for n in data["nodes"]}

    for node in pipeline.nodes:
        raw = by_id[node.id]
        if isinstance(node, AgentNode):
            raw["params"]["model"] = resolve_model(
                node.id,
                node.params.model,
                overrides=overrides,
                default_model=default_model,
            )
            _fill_retry_params(raw["params"], _agent_timeout(node.agent, agents))
        elif isinstance(node, LoopNode):
            raw["params"]["model"] = resolve_model(
                node.id,
                node.params.model,
                overrides=overrides,
                default_model=default_model,
            )
            # a chain keeps its list shape in the resolved snapshot; each element
            # gets its own effective model, keyed by its block name
            if isinstance(raw["body"], list):
                for i, block in enumerate(node.body_chain):
                    raw["body"][i]["model"] = resolve_model(
                        f"{node.id}.{node.body_block_name(i)}",
                        block.model,
                        overrides=overrides,
                        default_model=default_model,
                    )
            else:
                raw["body"]["model"] = resolve_model(
                    f"{node.id}.body",
                    node.body_chain[0].model,
                    overrides=overrides,
                    default_model=default_model,
                )
            raw["critic"]["model"] = resolve_model(
                f"{node.id}.critic",
                node.critic.model,
                overrides=overrides,
                default_model=default_model,
            )
            _fill_retry_params(
                raw["params"], _agent_timeout(node.body_last.agent, agents)
            )
        elif isinstance(node, DiscoverNode):
            # discover runs an agent step, so it needs an effective model too (§20.2)
            raw["params"]["model"] = resolve_model(
                node.id,
                node.params.model,
                overrides=overrides,
                default_model=default_model,
            )
            _fill_retry_params(raw["params"], _agent_timeout(node.agent, agents))
        elif isinstance(node, SelectNode):
            raw["selector"]["model"] = resolve_model(
                f"{node.id}.selector",
                node.selector.model,
                overrides=overrides,
                default_model=default_model,
            )
            _fill_retry_params(
                raw["params"], _agent_timeout(node.selector.agent, agents)
            )
        elif isinstance(node, BuiltinNode):
            pass  # builtin params are the node's own; no model to resolve
    return data


# --- snapshot assembly ------------------------------------------------------


@dataclass(frozen=True)
class SnapshotInfo:
    """Result of building a snapshot (SPEC §9)."""

    snapshot_dir: Path
    agents_lock: dict[str, str]


def _agent_dir_name(ref: str) -> str:
    """Library folder for ``name@version`` — the folder is the agent name (§6)."""
    return ref.split("@", 1)[0]


def build_snapshot(
    run_dir: Path | str,
    *,
    pipeline_path: Path | str,
    pipeline: Pipeline,
    library_path: Path | str,
    agents: dict[str, AgentSpec],
    overrides: dict[str, str] | None = None,
    default_model: str | None = None,
) -> SnapshotInfo:
    """Materialize ``<run_dir>/snapshot/`` from the live project + library (§9)."""
    run_dir = Path(run_dir)
    library_path = Path(library_path)
    overrides = overrides or {}
    snap = run_dir / SNAPSHOT_DIRNAME
    snap.mkdir(parents=True, exist_ok=True)

    # 1. verbatim pipeline copy
    shutil.copyfile(Path(pipeline_path), snap / "pipeline.yaml")

    # 2. resolved.yaml
    resolved = build_resolved(
        pipeline, agents=agents, overrides=overrides, default_model=default_model
    )
    (snap / "resolved.yaml").write_text(
        yaml.safe_dump(resolved, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 3. full copies of used agent packages + 4. lock hashes
    agents_root = snap / "agents"
    agents_root.mkdir(exist_ok=True)
    lock: dict[str, str] = {}
    for ref in used_agent_refs(pipeline):
        src = library_path / "agents" / _agent_dir_name(ref)
        if not src.is_dir():
            raise FileNotFoundError(f"agent package not found for {ref!r}: {src}")
        dest = agents_root / ref
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        lock[ref] = package_hash(dest)

    (snap / _LOCK_FILENAME).write_text(
        json.dumps(lock, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return SnapshotInfo(snapshot_dir=snap, agents_lock=lock)
