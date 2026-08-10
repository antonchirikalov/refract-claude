"""Input/output materialization, collections, and the gate (SPEC §10.1, §10.4, §10.2).

All filesystem linking goes through the single ``link_or_copy()`` helper
(symlink, with a copy fallback for Windows / unprivileged environments). Input
materialization lays out ``input/<port>/`` per §10.1; artifact naming follows
§10.4. The gate validates a step's produced non-optional ports (existence +
JSON schema + rules) and writes ``gate_report.json`` (§10.2).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from refract.models.types import ItemInfo, Rule, TypeKind
from refract.registry import ResolvedType, apply_rules, measure_rules

# --- the single linking helper (SPEC §10; Windows symlink fallback) --------


def long_path(path: Path | str) -> str:
    """A form of ``path`` the Windows APIs accept past the 260-character limit.

    Run trees nest deeply by design — ``runs/<run-id>/steps/<node>/<slug>/output/…``
    — and the slug and filename at the bottom come from an agent, not from us. A live
    discover node assembled a collection whose ``<slug>/<file>`` pair was 120
    characters on its own and crashed in ``shutil.copyfile`` with a bare
    ``FileNotFoundError``, which reads as "the agent produced nothing" rather than
    "the path was too long". The ``\\\\?\\`` prefix lifts the limit; it demands a
    fully-qualified path with no forward slashes and no ``..``, hence ``resolve()``.

    A no-op off Windows and for paths already prefixed.
    """
    text = os.fspath(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\" + os.fspath(Path(text).resolve())


def link_or_copy(src: Path | str, dst: Path | str) -> None:
    """Link ``src`` to ``dst``, falling back to a copy (SPEC §10, I-Windows).

    The ONLY place in the codebase that creates links. ``dst`` must not already
    exist; its parent is created. Directories are linked/copied recursively.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
        return
    except (OSError, NotImplementedError):
        pass
    if src.is_dir():
        shutil.copytree(long_path(src), long_path(dst))
    else:
        shutil.copy2(long_path(src), long_path(dst))


def make_tree_readonly(path: Path | str) -> None:
    """Best-effort: mark a materialized input tree read-only (SPEC §10.1)."""
    path = Path(path)
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    for target in targets:
        try:
            if target.is_symlink():
                continue
            mode = target.stat().st_mode
            target.chmod(mode & ~stat.S_IWRITE)
        except OSError:
            pass  # best-effort only


# --- artifact naming (SPEC §10.4) ------------------------------------------


def artifact_filename(port: str, rtype: ResolvedType) -> str:
    """Filename/dirname for a port's artifact (SPEC §10.4)."""
    if rtype.kind is TypeKind.file:
        ext = rtype.ext()
        return f"{port}{ext}" if ext else port
    return port  # dir/any → a directory named after the port


def artifact_path(base_dir: Path | str, port: str, rtype: ResolvedType) -> Path:
    """Full path of a port's artifact under ``base_dir`` (SPEC §10.4)."""
    return Path(base_dir) / artifact_filename(port, rtype)


# --- input materialization (SPEC §10.1) ------------------------------------


def _port_dir(input_root: Path | str, port: str) -> Path:
    d = Path(input_root) / port
    d.mkdir(parents=True, exist_ok=True)
    return d


def materialize_file(
    src_file: Path | str, input_root: Path | str, port: str, rtype: ResolvedType
) -> Path:
    """Single file artifact → ``input/<port>/<port>.<ext>`` (SPEC §10.1)."""
    port_dir = _port_dir(input_root, port)
    dst = port_dir / artifact_filename(port, rtype)
    link_or_copy(src_file, dst)
    return dst


def materialize_dir_or_any(src: Path | str, input_root: Path | str, port: str) -> Path:
    """kind=dir|any → content placed inside ``input/<port>/`` (SPEC §10.1).

    A file source is placed under its own name; a directory source has its
    contents placed directly inside the port dir.
    """
    src = Path(src)
    port_dir = _port_dir(input_root, port)
    if src.is_dir():
        for child in sorted(src.iterdir()):
            link_or_copy(child, port_dir / child.name)
    else:
        link_or_copy(src, port_dir / src.name)
    return port_dir


def materialize_collection(
    src_collection_dir: Path | str, input_root: Path | str, port: str
) -> Path:
    """Collection → ``input/<port>/`` with ``_collection.json`` + item dirs (§10.1)."""
    src_collection_dir = Path(src_collection_dir)
    port_dir = _port_dir(input_root, port)
    for child in sorted(src_collection_dir.iterdir()):
        link_or_copy(child, port_dir / child.name)
    return port_dir


def materialize_map_item(
    src_payload: Path | str, input_root: Path | str, port: str, item: ItemInfo
) -> Path:
    """Map element → payload + ``_item.json`` in ``input/<port>/`` (SPEC §10.1)."""
    src_payload = Path(src_payload)
    port_dir = _port_dir(input_root, port)
    if src_payload.is_dir():
        for child in sorted(src_payload.iterdir()):
            link_or_copy(child, port_dir / child.name)
    else:
        link_or_copy(src_payload, port_dir / src_payload.name)
    (port_dir / "_item.json").write_text(
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return port_dir


# --- the gate (SPEC §10.2 step 5) ------------------------------------------


@dataclass(frozen=True)
class GatePort:
    """A produced port to validate at the gate."""

    port: str
    rtype: ResolvedType
    optional: bool = False
    # node-level tightening on top of the type's own rules (SPEC §8 ``gate_rules``)
    extra_rules: tuple[Rule, ...] = ()


class PortGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: str
    ok: bool
    problems: list[str] = Field(default_factory=list)
    # what the rules measured, recorded on a PASS as well (SPEC §10.2): the verdict
    # alone cannot tell a document that cleared its floor by a hair from one that
    # cleared it fourfold, and only one of those is a finished artifact
    measures: dict[str, object] = Field(default_factory=dict)


class GateReport(BaseModel):
    """``gate_report.json`` — the outcome of gating a step's outputs (SPEC §10.2)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    ports: list[PortGateResult] = Field(default_factory=list)


def check_port(output_dir: Path | str, spec: GatePort) -> PortGateResult:
    """Validate one produced port: existence, JSON schema, rules (SPEC §10.2)."""
    path = artifact_path(output_dir, spec.port, spec.rtype)
    problems: list[str] = []
    measures: dict[str, object] = {}

    if not path.exists():
        return PortGateResult(port=spec.port, ok=False, problems=["output missing"])

    if spec.rtype.kind is TypeKind.file:
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return PortGateResult(
                port=spec.port, ok=False, problems=[f"unreadable: {e}"]
            )
        if spec.rtype.format is not None and spec.rtype.format.value == "json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                return PortGateResult(
                    port=spec.port, ok=False, problems=[f"invalid json: {e}"]
                )
            problems.extend(spec.rtype.validate_json(data))
        problems.extend(spec.rtype.check_rules(text))
        problems.extend(apply_rules(spec.extra_rules, text))
        measures = measure_rules(list(spec.rtype.rules) + list(spec.extra_rules), text)
    else:
        # dir/any: existence alone is too weak — an agent that produced nothing
        # would still pass (SPEC §10.2 > CHANGED). Require real content.
        if path.is_dir():
            # dot-entries are tooling artifacts, not content (as in §13): an agent
            # that wrote only a `.keep` produced nothing, and a discover agent that
            # found nothing must not pass as ok (SPEC §10.2 > CHANGED, §20.2).
            content = [c for c in path.iterdir() if not c.name.startswith(".")]
            if not content:
                problems.append("output directory has no content")
            # a directory that passed with one file is exactly the "silently thin
            # output" a run never reported (SPEC §10.2)
            measures = {"entries": len(content)}
        else:
            size = path.stat().st_size
            if size == 0:
                problems.append("output file is empty")
            measures = {"bytes": size}

    return PortGateResult(
        port=spec.port, ok=not problems, problems=problems, measures=measures
    )


def run_gate(output_dir: Path | str, ports: list[GatePort]) -> GateReport:
    """Gate all non-optional produced ports (SPEC §10.2 step 5)."""
    results = [check_port(output_dir, p) for p in ports if not p.optional]
    return GateReport(ok=all(r.ok for r in results), ports=results)


def write_gate_report(workdir: Path | str, report: GateReport) -> Path:
    """Persist ``gate_report.json`` in the step workdir (SPEC §10.2)."""
    path = Path(workdir) / "gate_report.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
