"""Pipeline load + validation and topological sort (SPEC §8).

All validation findings are collected as structured ``ValidationError`` records
(closed enum in §8.3), never raised one-by-one. Check order follows §8.3:
pydantic schema → ids/refs/existence → edge compatibility + map rules →
non-optional inputs connected → acyclicity (loop is an atomic vertex) → model
resolvability → §16 constraints → security warnings.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from refract.builtins import BUILTINS, BuiltinDef
from refract.models.agent import AgentSpec, Port
from refract.models.errors import Code, ValidationError
from refract.models.types import (
    ForbidFileRule,
    MinEntriesRule,
    NoEmptySectionsRule,
    ProseCharsRule,
)
from refract.models.pipeline import (
    AgentNode,
    BodyBlock,
    BuiltinNode,
    CriticBlock,
    DiscoverNode,
    LoopNode,
    Node,
    Pipeline,
    SelectNode,
)
from refract.registry import (
    ArtifactRegistry,
    check_edge,
    load_forbid_patterns,
    make_collection,
    parse_type_ref,
)

_RISKY_CAPABILITIES = ("bash", "webfetch")


# --- reference grammar (SPEC §8.1) -----------------------------------------


@dataclass(frozen=True)
class DataRef:
    """``<nodeId>.<outPort>`` — a data edge."""

    node_id: str
    port: str


@dataclass(frozen=True)
class BodyRef:
    """``@body`` or ``@body.<port>`` — loop-internal reference to body output."""

    port: str | None


@dataclass(frozen=True)
class PrevRef:
    """``@prev`` or ``@prev.<port>`` — output of the previous chain element (§10.3)."""

    port: str | None


@dataclass(frozen=True)
class BindingRef:
    """``@<selectId>.winner_model`` — the only scalar binding (SPEC §8.1)."""

    node_id: str


Ref = DataRef | BodyRef | PrevRef | BindingRef


def parse_ref(s: str) -> Ref | None:
    """Parse a reference string; ``None`` if it does not match the grammar."""
    for prefix, make in (("@body", BodyRef), ("@prev", PrevRef)):
        if s.startswith(prefix):
            rest = s[len(prefix) :]
            if rest == "":
                return make(None)
            if rest.startswith("."):
                return make(rest[1:])
            return None
    if s.startswith("@"):
        body = s[1:]
        if "." in body:
            node_id, attr = body.split(".", 1)
            if attr == "winner_model":
                return BindingRef(node_id)
        return None
    if "." in s:
        node_id, port = s.split(".", 1)
        return DataRef(node_id, port)
    return None


# --- agent library loading -------------------------------------------------


def load_agents(
    library_path: Path | str,
) -> tuple[dict[str, AgentSpec], list[ValidationError]]:
    """Load all agent packages from ``library/agents/*/agent.yaml`` (SPEC §6).

    Keys the result by ``name@version``. Malformed packages become ``E_SCHEMA``
    findings rather than exceptions.
    """
    agents: dict[str, AgentSpec] = {}
    errors: list[ValidationError] = []
    agents_dir = Path(library_path) / "agents"
    if not agents_dir.exists():
        return agents, errors
    for entry in sorted(agents_dir.iterdir()):
        agent_file = entry / "agent.yaml"
        if not entry.is_dir() or not agent_file.exists():
            continue
        try:
            raw = yaml.safe_load(agent_file.read_text("utf-8")) or {}
        except yaml.YAMLError as e:
            errors.append(
                ValidationError(code=Code.E_YAML, message=f"{agent_file}: {e}")
            )
            continue
        try:
            spec = AgentSpec.model_validate(raw)
        except PydanticValidationError as e:
            errors.append(
                ValidationError(code=Code.E_SCHEMA, message=f"{agent_file}: {e}")
            )
            continue
        agents[spec.ref] = spec
    return agents, errors


# --- validation context ----------------------------------------------------


@dataclass
class ValidationContext:
    """Everything the validator needs beyond the pipeline itself (SPEC §7, §8)."""

    registry: ArtifactRegistry
    agents: dict[str, AgentSpec]
    builtins: dict[str, BuiltinDef] = field(default_factory=lambda: BUILTINS)
    known_providers: set[str] = field(default_factory=set)
    available_providers: set[str] = field(default_factory=set)
    # Declared MCP servers (``~/.refract/mcp.yaml``). ``None`` means the MCP config was
    # not loaded, so the check is skipped — the CLI and API always pass the real set.
    known_mcp_servers: set[str] | None = None
    default_model: str | None = None
    model_overrides: dict[str, str] = field(default_factory=dict)


# --- parsing ---------------------------------------------------------------


def parse_pipeline_file(
    path: Path | str,
) -> tuple[Pipeline | None, list[ValidationError]]:
    """Parse + schema-validate a ``pipeline.yaml`` (SPEC §8, first check phase)."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text("utf-8")) or {}
    except yaml.YAMLError as e:
        return None, [ValidationError(code=Code.E_YAML, message=f"{path}: {e}")]
    try:
        pipeline = Pipeline.model_validate(raw)
    except PydanticValidationError as e:
        return None, [ValidationError(code=Code.E_SCHEMA, message=f"{path}: {e}")]
    return pipeline, []


# --- validator -------------------------------------------------------------


# The single output port a discover node exposes (SPEC §20.1) — the engine, not
# the agent, produces this collection.
DISCOVER_OUT_PORT = "sources"


class _Validator:
    """Runs the §8.3 check phases over a parsed pipeline, collecting findings."""

    def __init__(self, pipeline: Pipeline, ctx: ValidationContext) -> None:
        self.pipeline = pipeline
        self.ctx = ctx
        self.errors: list[ValidationError] = []
        self.nodes: dict[str, Node] = {}
        # memoized producer port types, keyed by node id
        self._ptypes: dict[str, dict[str, str]] = {}
        self._ptypes_active: set[str] = set()

    def err(self, code: Code, node_id: str | None, message: str) -> None:
        self.errors.append(ValidationError(code=code, node_id=node_id, message=message))

    # -- agent/spec helpers --

    def agent(self, ref: str) -> AgentSpec | None:
        return self.ctx.agents.get(ref)

    def _primary_produce(self, spec: AgentSpec) -> Port | None:
        non_optional = [p for p in spec.produces if not p.optional]
        if len(non_optional) != 1:
            return None
        return non_optional[0]

    # -- producer port types (with map/map_over wrapping) --

    def producer_types(self, node_id: str) -> dict[str, str]:
        if node_id in self._ptypes:
            return self._ptypes[node_id]
        if node_id in self._ptypes_active:  # cycle guard
            return {}
        self._ptypes_active.add(node_id)
        result = self._compute_producer_types(node_id)
        self._ptypes_active.discard(node_id)
        self._ptypes[node_id] = result
        return result

    def _compute_producer_types(self, node_id: str) -> dict[str, str]:
        node = self.nodes.get(node_id)
        if node is None:
            return {}
        if isinstance(node, BuiltinNode):
            bdef = self.ctx.builtins.get(node.builtin_name)
            if bdef is None:
                return {}
            return {p.port: p.type for p in bdef.produces}
        if isinstance(node, AgentNode):
            spec = self.agent(node.agent)
            if spec is None:
                return {}
            if node.map is not None or node.map_over is not None:
                # map/map_over output is collection<type of the primary port> (§8.1)
                primary = self._primary_produce(spec)
                if primary is None:
                    return {}
                return {primary.port: make_collection(primary.type)}
            return {p.port: p.type for p in spec.produces}
        if isinstance(node, LoopNode):
            # the LAST chain element produces the loop's draft (§10.3)
            body = self.agent(node.body_last.agent)
            out: dict[str, str] = {}
            if body is not None:
                by_port = {p.port: p.type for p in body.produces}
                primary = self._primary_produce(body)
                for local, ref_s in node.outputs.items():
                    ref = parse_ref(ref_s)
                    if isinstance(ref, BodyRef):
                        if ref.port is None and primary is not None:
                            out[local] = primary.type
                        elif ref.port is not None and ref.port in by_port:
                            out[local] = by_port[ref.port]
            return out
        if isinstance(node, DiscoverNode):
            # the engine assembles the agent's dir output into the collection (§20.1),
            # so the exposed port is fixed regardless of the agent's own port name
            return {DISCOVER_OUT_PORT: make_collection("source@v1")}
        if isinstance(node, SelectNode):
            src = parse_ref(node.candidates)
            if isinstance(src, DataRef):
                src_types = self.producer_types(src.node_id)
                cand_type = src_types.get(src.port)
                if cand_type is not None:
                    inner, is_coll = parse_type_ref(cand_type)
                    if is_coll:
                        return {"out": inner}
            return {}
        return {}

    # -- Phase A: duplicate ids --

    def _phase_ids(self) -> None:
        for node in self.pipeline.nodes:
            if node.id in self.nodes:
                self.err(Code.E_DUP_NODE_ID, node.id, f"duplicate node id {node.id!r}")
            else:
                self.nodes[node.id] = node

    # -- Phase B: existence of builtins, agents, types --

    def _phase_existence(self) -> None:
        for node in self.nodes.values():
            if isinstance(node, BuiltinNode):
                if node.builtin_name not in self.ctx.builtins:
                    self.err(Code.E_SCHEMA, node.id, f"unknown builtin {node.type!r}")
                    continue
                self._validate_builtin_params(node)
            elif isinstance(node, AgentNode):
                self._require_agent(node.id, node.agent)
            elif isinstance(node, LoopNode):
                for block in node.body_chain:
                    self._require_agent(node.id, block.agent)
                self._require_agent(node.id, node.critic.agent)
            elif isinstance(node, SelectNode):
                self._require_agent(node.id, node.selector.agent)
            elif isinstance(node, DiscoverNode):
                self._require_agent(node.id, node.agent)

    def _validate_builtin_params(self, node: BuiltinNode) -> None:
        bdef = self.ctx.builtins[node.builtin_name]
        try:
            bdef.params_model.model_validate(node.params)
        except PydanticValidationError as e:
            self.err(Code.E_SCHEMA, node.id, f"invalid builtin params: {e}")

    def _require_agent(self, node_id: str, ref: str) -> None:
        spec = self.agent(ref)
        if spec is None:
            self.err(Code.E_UNKNOWN_AGENT, node_id, f"unknown agent {ref!r}")
            return
        # types referenced by the agent must be known
        for port in [*spec.consumes, *spec.produces]:
            if not self.ctx.registry.knows_ref(port.type):
                self.err(
                    Code.E_UNKNOWN_TYPE,
                    node_id,
                    f"agent {ref!r} port {port.port!r} uses unknown type {port.type!r}",
                )
        # an MCP server the agent needs must be declared, or the step would launch
        # without the tool it was written around and fail (or silently not use it)
        declared = self.ctx.known_mcp_servers
        if declared is not None:
            for need in spec.needs:
                if need.startswith("mcp:") and need[len("mcp:") :] not in declared:
                    self.err(
                        Code.E_MCP_UNDECLARED,
                        node_id,
                        f"agent {ref!r} needs {need!r}, which is not declared in "
                        f"the MCP config",
                    )

    # -- Phase C: reference existence (node ids + ports) --

    def _phase_refs(self) -> None:
        for node in self.nodes.values():
            for _local, ref_s in self._data_inputs(node):
                self._check_data_ref(node.id, ref_s)
            if isinstance(node, AgentNode) and node.map is not None:
                self._check_data_ref(node.id, node.map)
            if isinstance(node, SelectNode):
                self._check_data_ref(node.id, node.candidates)

    def _data_inputs(self, node: Node) -> list[tuple[str, str]]:
        """External data-edge references on a node (excludes @body / map)."""
        pairs: list[tuple[str, str]] = []
        if isinstance(node, AgentNode | DiscoverNode):
            pairs.extend(node.inputs.items())
        elif isinstance(node, LoopNode):
            blocks: list[BodyBlock | CriticBlock] = [*node.body_chain, node.critic]
            for local, ref_s in [
                pair for block in blocks for pair in block.inputs.items()
            ]:
                if not ref_s.startswith("@"):
                    pairs.append((local, ref_s))
        return pairs

    def _check_data_ref(self, node_id: str | None, ref_s: str) -> None:
        ref = parse_ref(ref_s)
        if not isinstance(ref, DataRef):
            return
        if ref.node_id not in self.nodes:
            self.err(
                Code.E_UNKNOWN_NODE_REF,
                node_id,
                f"reference to unknown node {ref.node_id!r}",
            )
            return
        if ref.port not in self.producer_types(ref.node_id):
            self.err(
                Code.E_UNKNOWN_PORT,
                node_id,
                f"node {ref.node_id!r} has no output port {ref.port!r}",
            )

    def _refs_resolved(self) -> bool:
        blocking = {
            Code.E_DUP_NODE_ID,
            Code.E_UNKNOWN_AGENT,
            Code.E_UNKNOWN_NODE_REF,
            Code.E_UNKNOWN_PORT,
            Code.E_SCHEMA,
        }
        return not any(e.code in blocking for e in self.errors)

    def _phase_checkpoints(self) -> None:
        """Checkpoints must name nodes of this pipeline (SPEC §21.1)."""
        for node_id in self.pipeline.checkpoints:
            if node_id not in self.nodes:
                self.err(
                    Code.E_UNKNOWN_NODE_REF,
                    None,
                    f"checkpoint refers to unknown node {node_id!r}",
                )

    def _phase_outputs(self) -> None:
        """``outputs`` must name real ports of this pipeline (SPEC §22).

        Checked in the same phase as checkpoints and with the same codes as any other
        data reference: a delivery naming a port that does not exist is a run that
        finishes and hands over nothing, and finding that out after the run has been
        paid for is the whole failure this validation exists to prevent.
        """
        for name, ref_s in self.pipeline.outputs.items():
            ref = parse_ref(ref_s)
            if not isinstance(ref, DataRef):
                self.err(
                    Code.E_BINDING_ILLEGAL,
                    None,
                    f"output {name!r}: {ref_s!r} is not a <node>.<port> reference",
                )
                continue
            self._check_data_ref(None, ref_s)

    # -- Phase D: shape / §16 constraints --

    def _phase_shape(self) -> None:
        for node in self.nodes.values():
            if isinstance(node, AgentNode):
                self._shape_agent(node)
            elif isinstance(node, LoopNode):
                self._shape_loop(node)
            elif isinstance(node, SelectNode):
                self._shape_select(node)
            elif isinstance(node, DiscoverNode):
                self._shape_discover(node)

    def _reject_internal_refs(self, node_id: str, inputs: dict[str, str]) -> None:
        """``@body``/``@prev`` exist only inside a loop container (SPEC §8.1).

        Outside one they used to be silently ignored: the input simply stayed
        unconnected, and the agent ran without the artifact the author thought it
        had wired.
        """
        for local, ref_s in inputs.items():
            if isinstance(parse_ref(ref_s), BodyRef | PrevRef):
                self.err(
                    Code.E_LOOP_SHAPE,
                    node_id,
                    f"input {local!r} uses {ref_s!r}, which is only meaningful "
                    f"inside a loop container",
                )

    def _shape_agent(self, node: AgentNode) -> None:
        self._reject_internal_refs(node.id, node.inputs)
        if node.map is not None and node.map_over is not None:
            self.err(Code.E_MAP_CONFLICT, node.id, "node has both map and map_over")
        spec = self.agent(node.agent)
        if spec is None:
            return
        self._check_agent_contract(node.id, spec)
        self._check_gate_rules(node.id, spec, node.gate_rules)
        if node.map is not None:
            self._shape_map(node, spec)

    def _shape_discover(self, node: DiscoverNode) -> None:
        """A discover agent produces exactly one dir artifact (SPEC §20.1).

        The engine turns that directory into ``collection<source@v1>``; an agent with
        no dir port (or several primary ports) has nothing the engine can assemble.
        """
        self._reject_internal_refs(node.id, node.inputs)
        spec = self.agent(node.agent)
        if spec is None:
            return
        self._check_agent_contract(node.id, spec)
        primary = [p for p in spec.produces if not p.optional]
        if len(primary) != 1:
            self.err(
                Code.E_DISCOVER_SHAPE,
                node.id,
                f"discover agent {spec.ref!r} must have exactly one primary "
                f"produce port, got {len(primary)}",
            )
            return
        rtype = self.ctx.registry.get(primary[0].type)
        if rtype is not None and rtype.kind.value != "dir":
            self.err(
                Code.E_DISCOVER_SHAPE,
                node.id,
                f"discover agent {spec.ref!r} must produce a dir-kind artifact "
                f"(port {primary[0].port!r} is {rtype.kind.value})",
            )

    def _check_gate_rules(
        self, node_id: str, spec: AgentSpec, gate_rules: Sequence[object]
    ) -> None:
        """``gate_rules`` tighten the primary port, and a rule must fit its kind (§8).

        Text rules read the artifact's content, so on a ``dir``/``any`` port there is
        nothing to read and a rule that can never run would silently promise a
        guarantee. ``min_entries`` is the reverse case: it counts directory entries and
        is meaningless on a file. Each kind therefore accepts exactly the rules that can
        actually run against it.
        """
        if not gate_rules:
            return
        primary = [p for p in spec.produces if not p.optional]
        if not primary:
            self.err(
                Code.E_GATE_RULES_SHAPE,
                node_id,
                f"gate_rules on a node whose agent {spec.ref!r} produces nothing",
            )
            return
        rtype = self.ctx.registry.get(primary[0].type)
        if rtype is None:
            return
        dir_rules = [r for r in gate_rules if isinstance(r, MinEntriesRule)]
        text_rules = [r for r in gate_rules if not isinstance(r, MinEntriesRule)]
        if rtype.kind.value == "file" and dir_rules:
            self.err(
                Code.E_GATE_RULES_SHAPE,
                node_id,
                f"min_entries counts directory entries; port {primary[0].port!r} is a "
                "file artifact",
            )
        if rtype.kind.value != "file" and text_rules:
            self.err(
                Code.E_GATE_RULES_SHAPE,
                node_id,
                f"gate_rules apply to the primary port {primary[0].port!r}, which is "
                f"{rtype.kind.value}-kind — only min_entries applies to a directory",
            )
        # Rules that read MARKDOWN structure. On a JSON artifact they run and lie: braces
        # and keys are counted as prose, and every heading is missing because JSON has
        # none. Same class of mistake as `min_entries` on a file — a rule that cannot
        # mean what it says, silently promising a guarantee.
        markdown_rules = [
            r
            for r in gate_rules
            if isinstance(r, (ProseCharsRule, NoEmptySectionsRule))
        ]
        if markdown_rules and rtype.format is not None and rtype.format.value == "json":
            names = ", ".join(sorted({r.rule for r in markdown_rules}))
            self.err(
                Code.E_GATE_RULES_SHAPE,
                node_id,
                f"{names} read markdown structure; port {primary[0].port!r} is "
                "format=json — count its emptiness with the type's JSON schema instead",
            )
        self._check_forbid_files(node_id, gate_rules)

    def _check_forbid_files(self, node_id: str, rules: Sequence[object]) -> None:
        """A ``forbid_file`` list has to exist and hold patterns (§5).

        Checked here as well as at the gate, and that duplication is the point: a run
        whose pattern list went missing would otherwise discover it after paying for the
        step, and what it would see is a gate reporting no violations — which is what a
        clean draft looks like.
        """
        base = self.ctx.registry.library_path
        for rule in rules:
            if not isinstance(rule, ForbidFileRule):
                continue
            _, problems = load_forbid_patterns(Path(rule.path), base)
            for problem in problems:
                self.err(Code.E_FORBID_FILE, node_id, problem)

    def _check_agent_contract(self, node_id: str, spec: AgentSpec) -> None:
        # produces must not be a collection (I6, §16.7)
        for p in spec.produces:
            if parse_type_ref(p.type)[1]:
                self.err(
                    Code.E_AGENT_PRODUCES_COLLECTION,
                    node_id,
                    f"agent {spec.ref!r} produces collection on port {p.port!r}",
                )
        # HITL: at most one optional port, only question@v1 (§16.9)
        optional = [p for p in spec.produces if p.optional]
        if len(optional) > 1:
            self.err(Code.E_HITL_SHAPE, node_id, "more than one optional produce port")
        for p in optional:
            if p.type != "question@v1":
                self.err(
                    Code.E_HITL_SHAPE,
                    node_id,
                    f"optional port {p.port!r} must be question@v1",
                )

    def _shape_map(self, node: AgentNode, spec: AgentSpec) -> None:
        assert node.map is not None
        ref = parse_ref(node.map)
        if not isinstance(ref, DataRef) or ref.node_id not in self.nodes:
            return
        src = self.nodes[ref.node_id]
        # no nested map (§16.4): source must not be produced by a map/map_over node
        if isinstance(src, AgentNode) and (
            src.map is not None or src.map_over is not None
        ):
            self.err(
                Code.E_NESTED_MAP,
                node.id,
                f"map source {node.map!r} is produced by a map/map_over node",
            )
        src_type = self.producer_types(ref.node_id).get(ref.port)
        if src_type is None:
            return
        inner, is_coll = parse_type_ref(src_type)
        if not is_coll:
            self.err(
                Code.E_TYPE_MISMATCH,
                node.id,
                f"map source {node.map!r} is not a collection",
            )
            return
        # element binds to the single consumes port whose type == inner (§8.1)
        matching = [p for p in spec.consumes if p.type == inner]
        if len(matching) != 1:
            self.err(
                Code.E_MAP_PORT_AMBIGUOUS,
                node.id,
                f"agent has {len(matching)} consumes ports of type {inner!r} (need 1)",
            )

    def _shape_loop(self, node: LoopNode) -> None:
        critic = self.agent(node.critic.agent)
        if critic is not None:
            self._check_agent_contract(node.id, critic)
            self._check_gate_rules(node.id, critic, node.critic.gate_rules)
            primary = self._primary_produce(critic)
            if primary is None or primary.type != "verdict@v1":
                self.err(
                    Code.E_LOOP_SHAPE,
                    node.id,
                    "loop critic primary output must be verdict@v1",
                )
        for i, block in enumerate(node.body_chain):
            body = self.agent(block.agent)
            if body is not None:
                self._check_agent_contract(node.id, body)
                self._check_gate_rules(node.id, body, block.gate_rules)
            self._check_chain_refs(node, i, block.inputs)
        self._check_chain_refs(node, len(node.body_chain), node.critic.inputs)

    def _check_chain_refs(
        self, node: LoopNode, index: int, inputs: dict[str, str]
    ) -> None:
        """Internal refs must have something to point at (SPEC §10.3).

        ``index`` is the element's position in the chain; the critic is passed
        ``len(chain)`` because it sits after every body element. ``@prev`` in the
        first element and ``@body`` in a body element have no source — catching that
        here beats an engine crash mid-run.
        """
        for local, ref_s in inputs.items():
            ref = parse_ref(ref_s)
            if isinstance(ref, PrevRef) and index == 0:
                self.err(
                    Code.E_LOOP_SHAPE,
                    node.id,
                    f"input {local!r} of the first body element uses @prev, "
                    f"which has no previous element",
                )
            if isinstance(ref, BodyRef) and index < len(node.body_chain):
                self.err(
                    Code.E_LOOP_SHAPE,
                    node.id,
                    f"input {local!r} of a body element uses @body (the body's own "
                    f"output); use @prev for the previous element",
                )

    def _shape_select(self, node: SelectNode) -> None:
        selector = self.agent(node.selector.agent)
        if selector is not None:
            primary = self._primary_produce(selector)
            if primary is None or primary.type != "selection@v1":
                self.err(
                    Code.E_TYPE_MISMATCH,
                    node.id,
                    "select selector primary output must be selection@v1",
                )
        # candidates must be a collection
        ref = parse_ref(node.candidates)
        if isinstance(ref, DataRef):
            cand_type = self.producer_types(ref.node_id).get(ref.port)
            if cand_type is not None and not parse_type_ref(cand_type)[1]:
                self.err(
                    Code.E_TYPE_MISMATCH,
                    node.id,
                    f"select candidates {node.candidates!r} is not a collection",
                )

    # -- Phase E: edges + input completeness --

    def _phase_edges(self) -> None:
        for node in self.nodes.values():
            if isinstance(node, AgentNode):
                self._edges_agent(node)
            elif isinstance(node, LoopNode):
                self._edges_loop(node)
            elif isinstance(node, SelectNode):
                self._edges_select(node)
            elif isinstance(node, DiscoverNode):
                self._edges_discover(node)

    def _edge(
        self, node_id: str, ref_s: str, target_type: str, *, via_map: bool
    ) -> None:
        ref = parse_ref(ref_s)
        if not isinstance(ref, DataRef):
            return
        src_type = self.producer_types(ref.node_id).get(ref.port)
        if src_type is None:
            return
        code = check_edge(src_type, target_type, via_map=via_map)
        if code is not None:
            self.err(
                code,
                node_id,
                f"{ref_s} ({src_type}) incompatible with target {target_type}",
            )

    def _edges_discover(self, node: DiscoverNode) -> None:
        """Inputs of a discover node behave like a plain agent's (SPEC §20.1)."""
        spec = self.agent(node.agent)
        if spec is None:
            return
        consumes = {p.port: p.type for p in spec.consumes}
        for local, ref_s in node.inputs.items():
            if local not in consumes:
                self.err(
                    Code.E_UNKNOWN_PORT, node.id, f"unknown consumes port {local!r}"
                )
                continue
            self._edge(node.id, ref_s, consumes[local], via_map=False)
        for p in spec.consumes:
            if not p.optional and p.port not in node.inputs:
                self.err(
                    Code.E_INPUT_MISSING,
                    node.id,
                    f"required input port {p.port!r} is not connected",
                )

    def _edges_agent(self, node: AgentNode) -> None:
        spec = self.agent(node.agent)
        if spec is None:
            return
        consumes = {p.port: p.type for p in spec.consumes}
        map_port: str | None = None
        if node.map is not None:
            src_type = self._map_source_type(node)
            if src_type is not None:
                inner = parse_type_ref(src_type)[0]
                matching = [p for p in spec.consumes if p.type == inner]
                if len(matching) == 1:
                    map_port = matching[0].port
                    self._edge(node.id, node.map, matching[0].type, via_map=True)
        # explicit inputs
        for local, ref_s in node.inputs.items():
            if local not in consumes:
                self.err(
                    Code.E_UNKNOWN_PORT, node.id, f"unknown consumes port {local!r}"
                )
                continue
            self._edge(node.id, ref_s, consumes[local], via_map=False)
        # map_over does not consume a collection; ports come from inputs
        # completeness: every non-optional consumes port connected
        for p in spec.consumes:
            if p.optional:
                continue
            if p.port == map_port:
                continue
            if p.port not in node.inputs:
                self.err(
                    Code.E_INPUT_MISSING,
                    node.id,
                    f"consumes port {p.port!r} not connected",
                )

    def _map_source_type(self, node: AgentNode) -> str | None:
        if node.map is None:
            return None
        ref = parse_ref(node.map)
        if not isinstance(ref, DataRef):
            return None
        return self.producer_types(ref.node_id).get(ref.port)

    def _edges_loop(self, node: LoopNode) -> None:
        for block in node.body_chain:
            self._edges_block_inputs(node.id, block.agent, block.inputs)
        self._edges_block_inputs(node.id, node.critic.agent, node.critic.inputs)

    def _edges_block_inputs(
        self, node_id: str, agent_ref: str, inputs: dict[str, str]
    ) -> None:
        spec = self.agent(agent_ref)
        if spec is None:
            return
        consumes = {p.port: p.type for p in spec.consumes}
        for local, ref_s in inputs.items():
            if local not in consumes:
                self.err(
                    Code.E_UNKNOWN_PORT, node_id, f"unknown consumes port {local!r}"
                )
                continue
            if ref_s.startswith("@"):
                continue  # @body etc. resolved by the engine at runtime
            self._edge(node_id, ref_s, consumes[local], via_map=False)
        for p in spec.consumes:
            if not p.optional and p.port not in inputs:
                self.err(
                    Code.E_INPUT_MISSING,
                    node_id,
                    f"consumes port {p.port!r} not connected",
                )

    def _edges_select(self, node: SelectNode) -> None:
        selector = self.agent(node.selector.agent)
        if selector is None:
            return
        ref = parse_ref(node.candidates)
        if not isinstance(ref, DataRef):
            return
        cand_type = self.producer_types(ref.node_id).get(ref.port)
        if cand_type is None:
            return
        # bind candidates to the selector's single collection consumes port
        coll_ports = [p for p in selector.consumes if parse_type_ref(p.type)[1]]
        if len(coll_ports) == 1:
            self._edge(node.id, node.candidates, coll_ports[0].type, via_map=False)

    # -- Phase F: acyclicity (loop is atomic) --

    def _deps(self) -> dict[str, set[str]]:
        deps: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        for node in self.nodes.values():
            refs: list[str] = [r for _, r in self._data_inputs(node)]
            if isinstance(node, AgentNode) and node.map is not None:
                refs.append(node.map)
            if isinstance(node, SelectNode):
                refs.append(node.candidates)
            # scalar binding dependencies (§8.1) — any sub-block model may bind
            for binding in self._binding_refs(node):
                if binding in self.nodes:
                    deps[node.id].add(binding)
            for ref_s in refs:
                ref = parse_ref(ref_s)
                if isinstance(ref, DataRef) and ref.node_id in self.nodes:
                    deps[node.id].add(ref.node_id)
        return deps

    def _binding_refs(self, node: Node) -> set[str]:
        models: list[str | None] = []
        if isinstance(node, LoopNode):
            models += [b.model for b in node.body_chain]
            models.append(node.critic.model)
        elif isinstance(node, AgentNode):
            models.append(node.params.model)
        elif isinstance(node, SelectNode):
            models.append(node.selector.model)
        out: set[str] = set()
        for model in models:
            if isinstance(model, str):
                ref = parse_ref(model)
                if isinstance(ref, BindingRef):
                    out.add(ref.node_id)
        return out

    def toposort(self) -> list[str]:
        deps = self._deps()
        indeg = {n: len(d) for n, d in deps.items()}
        children: dict[str, list[str]] = {n: [] for n in deps}
        for n, d in deps.items():
            for parent in d:
                children[parent].append(n)
        queue = deque(sorted(n for n, k in indeg.items() if k == 0))
        order: list[str] = []
        while queue:
            n = queue.popleft()
            order.append(n)
            for c in sorted(children[n]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if len(order) != len(self.nodes):
            remaining = sorted(set(self.nodes) - set(order))
            self.err(Code.E_CYCLE, None, f"cycle involving nodes: {remaining}")
        return order

    # -- Phase G: model resolution --

    def _phase_models(self) -> None:
        for node in self.nodes.values():
            if isinstance(node, AgentNode):
                self._resolve_model(node.id, node.id, node.params.model)
            elif isinstance(node, LoopNode):
                for i, block in enumerate(node.body_chain):
                    key = f"{node.id}.{node.body_block_name(i)}"
                    self._resolve_model(node.id, key, block.model)
                self._resolve_model(node.id, f"{node.id}.critic", node.critic.model)
            elif isinstance(node, SelectNode):
                self._resolve_model(node.id, f"{node.id}.selector", node.selector.model)
            elif isinstance(node, DiscoverNode):
                self._resolve_model(node.id, node.id, node.params.model)

    def _resolve_model(self, node_id: str, key: str, node_model: str | None) -> None:
        override = self.ctx.model_overrides.get(key)
        # scalar binding: validate the referenced select's map_over providers
        if node_model is not None and node_model.startswith("@"):
            ref = parse_ref(node_model)
            if not isinstance(ref, BindingRef):
                self.err(
                    Code.E_BINDING_ILLEGAL, node_id, f"illegal binding {node_model!r}"
                )
                return
            self._validate_binding_source(node_id, ref.node_id)
            return
        model = override or node_model or self.ctx.default_model
        if model is None:
            self.err(Code.E_MODEL_UNRESOLVED, node_id, f"no model for {key}")
            return
        self._check_provider(node_id, model)

    def _validate_binding_source(self, node_id: str, select_id: str) -> None:
        src = self.nodes.get(select_id)
        if not isinstance(src, SelectNode):
            self.err(
                Code.E_BINDING_ILLEGAL,
                node_id,
                f"winner_model binding requires a select node, got {select_id!r}",
            )
            return
        cand = parse_ref(src.candidates)
        cand_node = self.nodes.get(cand.node_id) if isinstance(cand, DataRef) else None
        if not isinstance(cand_node, AgentNode) or cand_node.map_over is None:
            self.err(
                Code.E_BINDING_ILLEGAL,
                node_id,
                "winner_model exists only when candidates come from map_over.models",
            )
            return
        for model in cand_node.map_over.models:
            self._check_provider(node_id, model)

    def _check_provider(self, node_id: str, model: str) -> None:
        provider = model.split("/", 1)[0]
        if provider not in self.ctx.known_providers:
            self.err(
                Code.E_PROVIDER_UNAVAILABLE,
                node_id,
                f"provider {provider!r} is not configured",
            )
        elif provider not in self.ctx.available_providers:
            self.err(
                Code.E_PROVIDER_UNAVAILABLE,
                node_id,
                f"provider {provider!r} is unavailable (env key empty)",
            )

    # -- Phase H: warnings (cache, security) --

    def _phase_warnings(self) -> None:
        self._warn_cache()
        self._warn_security()
        self._warn_thresholds()

    def _warn_cache(self) -> None:
        for node in self.nodes.values():
            cache = getattr(getattr(node, "params", None), "cache", False)
            if cache:
                self.err(
                    Code.W_CACHE_UNSUPPORTED, node.id, "cache is unsupported in v0.1"
                )

    def _warn_thresholds(self) -> None:
        """A map node's ``min_ok`` against the floor of the discover node feeding it.

        ``min_sources`` is a FLOOR, so a larger ``min_ok`` is not provably unsatisfiable
        — it passes whenever the finder over-delivers, which is why this is a warning and
        not an error. It is still a latent failure: the run's success depends on the
        finder exceeding its own floor, and when the finder is capped against duplicates
        (a textbook subject has one primary source and a pile of restatements) that is a
        bet the pipeline loses. Equality is the same bet with zero tolerance: one
        unreadable page fails a node that had everything it needed.

        Two live runs paid for this. One demanded 10 notes from a shelf floored at 8 and
        only passed because the search happened to return 16. Another demanded 12 sources
        on a subject where the finder's own cap stopped it at 8 — every required point
        covered twice over, and the node failed on the threshold.
        """
        for node in self.nodes.values():
            if not isinstance(node, AgentNode) or not node.map:
                continue
            source_id = node.map.split(".")[0]
            source = self.nodes.get(source_id)
            if not isinstance(source, DiscoverNode):
                continue
            min_ok = node.params.min_ok
            floor = source.params.min_sources
            if min_ok > floor:
                self.err(
                    Code.W_THRESHOLDS,
                    node.id,
                    f"min_ok {min_ok} exceeds min_sources {floor} of {source_id!r}: "
                    "the node can only pass if the finder delivers above its floor",
                )
            elif min_ok == floor and floor > 1:
                # Only when someone SET a non-trivial floor. Both params default to 1,
                # and warning on the engine's own defaults would put a warning on every
                # research pipeline out of the box — noise that devalues the real ones.
                self.err(
                    Code.W_THRESHOLDS,
                    node.id,
                    f"min_ok {min_ok} equals min_sources {floor} of {source_id!r}: "
                    "one unusable source fails the node",
                )

    def _warn_security(self) -> None:
        deps = self._deps()
        children: dict[str, list[str]] = {n: [] for n in deps}
        for n, d in deps.items():
            for parent in d:
                children[parent].append(n)
        # descendants reachable from any scanner node
        reachable: set[str] = set()
        queue: deque[str] = deque(
            n
            for n, node in self.nodes.items()
            if isinstance(node, BuiltinNode) and node.builtin_name == "scanner"
        )
        while queue:
            n = queue.popleft()
            for c in children[n]:
                if c not in reachable:
                    reachable.add(c)
                    queue.append(c)
        for node_id in sorted(reachable):
            for spec in self._node_agents(self.nodes[node_id]):
                if self._has_risky_capability(spec):
                    self.err(
                        Code.W_SECURITY,
                        node_id,
                        "node reachable from scanner uses bash/webfetch/mcp",
                    )
                    break

    def _node_agents(self, node: Node) -> list[AgentSpec]:
        refs: list[str] = []
        if isinstance(node, AgentNode):
            refs = [node.agent]
        elif isinstance(node, LoopNode):
            refs = [*(b.agent for b in node.body_chain), node.critic.agent]
        elif isinstance(node, SelectNode):
            refs = [node.selector.agent]
        return [s for s in (self.agent(r) for r in refs) if s is not None]

    def _has_risky_capability(self, spec: AgentSpec) -> bool:
        return any(
            need in _RISKY_CAPABILITIES or need.startswith("mcp:")
            for need in spec.needs
        )

    # -- driver --

    def run(self) -> list[str]:
        self._phase_ids()
        self._phase_existence()
        self._phase_refs()
        self._phase_checkpoints()
        self._phase_outputs()
        if self._refs_resolved():
            self._phase_shape()
            self._phase_edges()
        order = self.toposort()
        self._phase_models()
        self._phase_warnings()
        return order


# --- public API ------------------------------------------------------------


@dataclass
class LoadedGraph:
    """Result of loading + validating a pipeline (SPEC §8)."""

    pipeline: Pipeline | None
    order: list[str]
    errors: list[ValidationError]

    @property
    def ok(self) -> bool:
        """True if there are no blocking (non-warning) errors."""
        return not any(not e.code.is_warning for e in self.errors)


def validate_pipeline(
    pipeline: Pipeline, ctx: ValidationContext
) -> tuple[list[str], list[ValidationError]]:
    """Validate a parsed pipeline; return ``(topological_order, errors)`` (SPEC §8.3)."""
    validator = _Validator(pipeline, ctx)
    order = validator.run()
    return order, validator.errors


def load_pipeline(path: Path | str, ctx: ValidationContext) -> LoadedGraph:
    """Parse a ``pipeline.yaml`` file and validate it (SPEC §8)."""
    pipeline, errors = parse_pipeline_file(path)
    if pipeline is None:
        return LoadedGraph(pipeline=None, order=[], errors=errors)
    order, verrors = validate_pipeline(pipeline, ctx)
    return LoadedGraph(pipeline=pipeline, order=order, errors=errors + verrors)
