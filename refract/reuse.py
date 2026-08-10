"""Reuse / rerun-from-node support (SPEC §10.5).

A rerun is a NEW run that copies unchanged step outputs from a previous run
instead of re-executing them. The recompute set is
``R = {NODE} ∪ descendants(NODE)``; builtins always execute; map elements are
diffed by ``(slug, source_hash)``. Nodes outside R with unchanged inputs are
reused wholesale (their steps become ``reused``, artifacts ``link_or_copy``'d).

Pure helpers here; the scheduler drives them (it owns the ledger + events).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from refract.artifacts import link_or_copy
from refract.state import read_state
from refract.models.ledger import RunState


def load_run_state(run_dir: Path) -> RunState:
    """Read a prior run's ``state.json`` verbatim (no crash recovery)."""
    raw = read_state(run_dir)
    return RunState.model_validate(raw)


def descendants(deps: dict[str, set[str]], seed: set[str]) -> set[str]:
    """All nodes transitively downstream of ``seed`` given child→parents ``deps``."""
    children: dict[str, set[str]] = {nid: set() for nid in deps}
    for child, parents in deps.items():
        for parent in parents:
            children.setdefault(parent, set()).add(child)
    out: set[str] = set()
    stack = [c for s in seed for c in children.get(s, set())]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, set()))
    return out


def recompute_set(deps: dict[str, set[str]], force_nodes: list[str]) -> set[str]:
    """``R = force_nodes ∪ descendants(force_nodes)`` (SPEC §10.5)."""
    force = set(force_nodes)
    return force | descendants(deps, force)


def copy_tree_linked(src: Path, dst: Path) -> None:
    """Recreate ``src`` at ``dst`` reusing files via :func:`link_or_copy` (§10.5)."""
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if child.is_dir():
            copy_tree_linked(child, target)
        else:
            link_or_copy(child, target)


def map_reuse_index(reuse_run_dir: Path, node_id: str, out_port: str) -> dict[str, str]:
    """``{slug: source_hash}`` of ok elements in a prior map node's output (§10.5).

    Used to decide per-element reuse: an input item reuses its old step when its
    ``(slug, source_hash)`` matches an ok element of the reuse run.
    """
    manifest = (
        Path(reuse_run_dir) / "steps" / node_id / "_out" / out_port / "_collection.json"
    )
    if not manifest.exists():
        return {}
    data = json.loads(manifest.read_text("utf-8"))
    return {
        item["slug"]: item.get("source_hash")
        for item in data.get("items", [])
        if item.get("status") == "ok"
    }


def builtin_signature(output_base: Path, port: str) -> str:
    """A stable content signature of a builtin's output port for change detection.

    For a collection port it is the sorted ``slug:source_hash`` lines; for a plain
    file or directory output, a hash of the content. Empty string only when the port
    produced nothing recognisable — and an empty signature counts as "changed", so
    the fallback is safe but expensive.

    Only collections were understood at first, which quietly disabled reuse for every
    brief-driven pipeline: ``builtin/brief`` writes ``<port>.md`` and has no manifest,
    so its signature was always empty, it always counted as changed, and ``rerun
    --from`` re-searched and re-extracted everything downstream on every run.
    """
    manifest = output_base / port / "_collection.json"
    if manifest.exists():
        data = json.loads(manifest.read_text("utf-8"))
        lines = sorted(
            f"{item.get('slug')}:{item.get('source_hash')}:{item.get('status')}"
            for item in data.get("items", [])
        )
        return "\n".join(lines)

    # a file artifact: <port>.<ext> written next to the port dir (SPEC §10.4)
    files = (
        sorted(p for p in output_base.glob(f"{port}.*") if p.is_file())
        if output_base.is_dir()
        else []
    )
    port_dir = output_base / port
    if port_dir.is_dir():
        files += sorted(p for p in port_dir.rglob("*") if p.is_file())
    if not files:
        return ""
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
