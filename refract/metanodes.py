"""Loop and select meta-node execution (SPEC §10.3, Phase 1).

Both reuse the single step lifecycle in :mod:`refract.steps` — they only add the
control flow around it. The loop round number is DERIVED from the ledger (never
stored); output assembly under ``steps/<id>/_out/`` is idempotent so resume
re-assembles cleanly, exactly like map.

The scheduler owns provider semaphores, the ledger and event emission; it hands
them here in a :class:`MetaContext` so this module never imports the scheduler
(which imports it).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from refract.artifacts import (
    artifact_filename,
    artifact_path,
    link_or_copy,
    long_path,
    node_output_base,
)
from refract.graph import BodyRef, DataRef, PrevRef, parse_ref
from refract.models.agent import AgentSpec, Port
from refract.models.ledger import NodeStatus, StepOutcome, StepState, StepStatus
from refract.models.pipeline import (
    BodyBlock,
    CriticBlock,
    LoopNode,
    Node,
    SelectNode,
)
from refract.models.types import CollectionManifest, CollectionStatus
from refract.prompt import RevisionContext
from refract.registry import ArtifactRegistry, model_slug, parse_type_ref
from refract.runtime.base import AgentRuntime
from refract.state import Ledger
from refract.steps import (
    AgentStepPlan,
    AuxFileInput,
    CollectionInput,
    DirAnyInput,
    FileInput,
    InputSpec,
    execute_agent_step,
)

_VERDICT_TYPE = "verdict@v1"
_SELECTION_TYPE = "selection@v1"


@dataclass
class MetaContext:
    """Execution handles the scheduler lends to loop/select (SPEC §10.5)."""

    run_dir: Path
    agents: dict[str, AgentSpec]
    registry: ArtifactRegistry
    runtime: AgentRuntime
    ledger: Ledger
    nodes: dict[str, Node]
    clock: Callable[[], str]
    sleeper: Callable[[float], Awaitable[None]]
    emit_event: Callable[[dict[str, object]], None]
    set_node: Callable[..., None]
    semaphore_for: Callable[[str], asyncio.Semaphore]
    resolve_inputs: Callable[[AgentSpec, dict[str, str], str], list[InputSpec]]
    resolve_model: Callable[[str], str]

    def agent_dir(self, ref: str) -> Path:
        return self.run_dir / "snapshot" / "agents" / ref

    def output_base(self, node_id: str) -> Path:
        """Directory holding a producer node's port outputs (SPEC §9/§10.3)."""
        return node_output_base(self.run_dir, self.nodes[node_id])


def _primary(agent: AgentSpec) -> Port:
    non_optional = [p for p in agent.produces if not p.optional]
    if len(non_optional) != 1:
        raise ValueError(f"agent {agent.name!r} has no single primary output")
    return non_optional[0]


def _warn(ctx: MetaContext, node_id: str, message: str) -> None:
    ctx.emit_event(
        {
            "type": "log",
            "payload": {"level": "warning", "node_id": node_id, "message": message},
        }
    )


def _fail(ctx: MetaContext, node_id: str, error: str) -> NodeStatus:
    ctx.set_node(node_id, NodeStatus.failed, error=error)
    return NodeStatus.failed


async def _run_step(ctx: MetaContext, plan: AgentStepPlan) -> StepState:
    """Run one sub-step, reusing an already-``done`` ledger step on resume (§10.5)."""
    existing = ctx.ledger.get_step(plan.step_id)
    if existing is not None and existing.status is StepStatus.done:
        return existing
    async with ctx.semaphore_for(plan.model):
        step = await execute_agent_step(
            plan,
            ctx.runtime,
            ctx.ledger,
            on_event=ctx.emit_event,
            clock=ctx.clock,
            sleeper=ctx.sleeper,
        )
    if step.status is StepStatus.waiting_human:
        raise NotImplementedError(
            f"HITL (question@v1) inside a loop/select block is not supported "
            f"({plan.step_id}); use a plain agent node for interactive steps."
        )
    return step


def _port_input(
    ctx: MetaContext, agent: AgentSpec, port: str, src_port: str, src_out: Path
) -> InputSpec:
    """One InputSpec for ``port`` sourced from ``src_out/<src_port>`` (§10.1/§10.4)."""
    ptype = {p.port: p.type for p in agent.consumes}[port]
    if ptype.startswith("collection<"):
        return CollectionInput(port=port, src=src_out / src_port)
    rtype = ctx.registry.get(ptype)
    if rtype is None:
        raise KeyError(f"unknown type {ptype!r} for port {port!r}")
    if rtype.kind.value == "file":
        return FileInput(
            port=port, src=artifact_path(src_out, src_port, rtype), rtype=rtype
        )
    return DirAnyInput(port=port, src=src_out / src_port)


# --- loop (SPEC §10.3) ------------------------------------------------------


async def run_loop(node: LoopNode, ctx: MetaContext) -> NodeStatus:
    """Run ``body…:r1 → critic:r1 → [body…:r2 …]`` until approved or max_rounds.

    ``body`` may be a CHAIN of elements (SPEC §10.3): they run in order within a
    round, each seeing the previous one's output via ``@prev``, and the last one's
    output is the round's draft — what the critic judges and what ``@body`` means.
    The control decision stays one verdict from one critic.
    """
    chain = node.body_chain
    last_agent = ctx.agents[node.body_last.agent]
    critic_agent = ctx.agents[node.critic.agent]
    critic_model_raw = node.critic.model or node.params.model
    assert critic_model_raw is not None
    critic_model = ctx.resolve_model(critic_model_raw)

    ctx.set_node(node.id, NodeStatus.running)

    approved_round: int | None = None
    plateau_round: int | None = None
    # The fewest open items any round has managed, and how many rounds have failed to beat
    # it. Derived here rather than stored: the ledger holds the verdicts, this is arithmetic
    # over them.
    best_open = 1 << 30
    best_round = 1
    since_best = 0
    last_round = 0
    for r in range(1, node.params.max_rounds + 1):
        last_round = r
        prev_out: Path | None = None
        for i, block in enumerate(chain):
            name = node.body_block_name(i)
            agent = ctx.agents[block.agent]
            model_raw = block.model or node.params.model
            assert model_raw is not None
            inputs, internal = _chain_inputs(ctx, node, agent, block, name, prev_out)
            revision: RevisionContext | None = None
            if i == 0 and r >= 2:
                # only the first element revises: it is the one that rewrites from
                # the previous round's draft plus the verdict
                aux, revision = _revision(
                    ctx, node, last_agent, _primary(last_agent), r
                )
                inputs += aux
            workdir = ctx.run_dir / "steps" / node.id / f"{name}_r{r}"
            step = await _run_step(
                ctx,
                _plan(
                    ctx,
                    step_id=f"{node.id}.{name}:r{r}",
                    node=node,
                    workdir=workdir,
                    agent=agent,
                    agent_ref=block.agent,
                    model=ctx.resolve_model(model_raw),
                    inputs=inputs + internal,
                    block=block,
                    revision=revision,
                ),
            )
            if step.outcome is not StepOutcome.ok:
                return _fail(ctx, node.id, f"loop {name} r{r}: {_outcome(step)}")
            prev_out = workdir / "output"

        assert prev_out is not None  # the chain has at least one element
        body_out = prev_out
        critic = await _run_step(
            ctx,
            _plan(
                ctx,
                step_id=f"{node.id}.critic:r{r}",
                node=node,
                workdir=ctx.run_dir / "steps" / node.id / f"critic_r{r}",
                agent=critic_agent,
                agent_ref=node.critic.agent,
                model=critic_model,
                inputs=_critic_inputs(ctx, node, critic_agent, last_agent, body_out),
                block=node.critic,
                revision=None,
            ),
        )
        if critic.outcome is not StepOutcome.ok:
            return _fail(ctx, node.id, f"loop critic r{r}: {_outcome(critic)}")

        if _read_verdict(ctx, node, critic_agent, r) == "approved":
            approved_round = r
            break

        # Plateau: a round that did not beat the fewest open items any round has managed.
        # Two in a row and the loop stops — one bad round is noise, two is a shape. The
        # draft still passes on and its open items are still reported; this only declines
        # to pay for a third answer that is not getting shorter.
        # A `revise` verdict that names no issues is UNMEASURABLE, not zero. `issues` is
        # optional in verdict@v1 and four of the shipped critics never mention it, so
        # counting the absence as 0 made round 1 unbeatable: every later round failed to
        # improve on nothing, the plateau fired, and the FIRST draft shipped while every
        # round had been paid for. Skipping the accounting costs a round of budget and
        # cannot ship the wrong draft.
        issues = _verdict_issues(ctx, node, critic_agent, r)
        if not issues:
            _warn(
                ctx,
                node.id,
                f"round {r}: the critic asked for a revision but named no issues, so "
                "there is nothing to measure — the plateau check skips this round",
            )
            continue
        open_now = len(issues)
        if open_now < best_open:
            best_open, best_round, since_best = open_now, r, 0
        else:
            since_best += 1
            _warn(
                ctx,
                node.id,
                f"round {r}: {open_now} open item(s), no better than {best_open} "
                f"at round {best_round} ({since_best} in a row without improvement)",
            )
        limit = node.params.plateau_rounds
        if limit is not None and since_best >= limit:
            plateau_round = r
            _warn(
                ctx,
                node.id,
                f"plateau: {since_best} round(s) without improving on {best_open} "
                f"open item(s); stopping with {node.params.max_rounds - r} round(s) unspent",
            )
            break

    if approved_round is not None:
        chosen = approved_round
    elif node.params.on_max_rounds == "fail":
        # A declared failure outranks the plateau. The plateau decides WHEN to stop paying
        # for rounds; `on_max_rounds` decides what an unapproved draft is worth, and a
        # pipeline that said "unapproved means failed" does not change its mind because the
        # loop stopped early. Reading these the other way round turned a declared failure
        # into a `done` node.
        stopped = (
            f"plateau after {plateau_round} round(s)"
            if plateau_round is not None
            else f"{node.params.max_rounds} rounds"
        )
        return _fail(ctx, node.id, f"loop: {stopped} without approval")
    elif plateau_round is not None:
        # The loop stopped itself. Take the best round rather than the last: the point of
        # counting was that the last one is not the best.
        chosen = best_round
    else:  # pass: take the last round's draft, warn (SPEC §10.3)
        chosen = last_round
        _warn(ctx, node.id, f"max_rounds ({node.params.max_rounds}) reached; passing")

    _assemble_loop_output(ctx, node, last_agent, chosen)
    open_items = _write_unresolved(
        ctx, node, critic_agent, chosen=chosen, approved=approved_round is not None
    )
    if open_items:
        _warn(
            ctx,
            node.id,
            f"{open_items} open item(s) from round {chosen}: "
            f"steps/{node.id}/_out/unresolved.md",
        )
    ctx.set_node(node.id, NodeStatus.done)
    return NodeStatus.done


def _chain_inputs(
    ctx: MetaContext,
    node: LoopNode,
    agent: AgentSpec,
    block: BodyBlock,
    block_name: str,
    prev_out: Path | None,
) -> tuple[list[InputSpec], list[InputSpec]]:
    """Split a chain element's inputs into external edges and ``@prev`` ones.

    Returns ``(external, internal)``; the validator has already rejected ``@prev``
    on the first element, so ``prev_out`` is never ``None`` where it is needed.
    """
    index = [node.body_block_name(i) for i in range(len(node.body_chain))].index(
        block_name
    )
    data_refs: dict[str, str] = {}
    internal: list[InputSpec] = []
    for port, ref_s in block.inputs.items():
        ref = parse_ref(ref_s)
        if isinstance(ref, PrevRef):
            assert prev_out is not None and index > 0
            previous = ctx.agents[node.body_chain[index - 1].agent]
            src_port = ref.port or _primary(previous).port
            internal.append(_port_input(ctx, agent, port, src_port, prev_out))
        else:
            data_refs[port] = ref_s
    external = list(ctx.resolve_inputs(agent, data_refs, f"{node.id}.{block_name}"))
    return external, internal


def _outcome(step: StepState) -> str:
    return step.outcome.value if step.outcome is not None else "no outcome"


def _plan(
    ctx: MetaContext,
    *,
    step_id: str,
    node: LoopNode,
    workdir: Path,
    agent: AgentSpec,
    agent_ref: str,
    model: str,
    inputs: list[InputSpec],
    block: BodyBlock | CriticBlock,
    revision: RevisionContext | None,
) -> AgentStepPlan:
    """Build the step plan for one element of a loop.

    ``block`` is the BLOCK, not its ``params``. It used to be the params, and the
    difference was silent: ``SubBlockParams`` has no ``gate_rules`` field, so
    ``getattr(block, "gate_rules", [])`` returned an empty list for every body and every
    critic — declared rules validated, resolved into ``resolved.yaml``, and then vanished
    at the last handoff. Measured live: a writer whose node asked for 8 000-12 000
    characters of prose was never told so (the bound is generated into the prompt from
    the same list) and produced 24 812, which the gate then passed because it was
    checking the type's rules alone.
    """
    params = block.params

    def pick(field: str, fallback: int) -> int:
        val = getattr(params, field, None) if params is not None else None
        if val is not None:
            return int(val)
        loop_val = getattr(node.params, field, None)
        return int(loop_val) if loop_val is not None else fallback

    timeout = pick("timeout_s", 0) or agent.defaults.timeout_s
    return AgentStepPlan(
        step_id=step_id,
        node_id=node.id,
        workdir=workdir,
        agent=agent,
        agent_dir=ctx.agent_dir(agent_ref),
        model=model,
        registry=ctx.registry,
        inputs=inputs,
        timeout_s=timeout,
        gate_retries=pick("gate_retries", 2),
        infra_retries=pick("infra_retries", 2),
        revision=revision,
        # a loop's body/critic block may tighten its own gate (SPEC §8). Read off the
        # BLOCK: reading it off `block.params` is how these silently never ran.
        gate_rules=list(block.gate_rules),
    )


def _critic_inputs(
    ctx: MetaContext,
    node: LoopNode,
    critic_agent: AgentSpec,
    body_agent: AgentSpec,
    body_out: Path,
) -> list[InputSpec]:
    body_primary = _primary(body_agent)
    data_refs: dict[str, str] = {}
    specs: list[InputSpec] = []
    for port, ref_s in node.critic.inputs.items():
        ref = parse_ref(ref_s)
        if isinstance(ref, BodyRef):  # @body / @body.<port> → this round's draft
            src_port = ref.port or body_primary.port
            specs.append(_port_input(ctx, critic_agent, port, src_port, body_out))
        else:
            data_refs[port] = ref_s
    specs += ctx.resolve_inputs(critic_agent, data_refs, f"{node.id}.critic")
    return specs


def _revision(
    ctx: MetaContext,
    node: LoopNode,
    body_agent: AgentSpec,
    body_primary: Port,
    r: int,
) -> tuple[list[InputSpec], RevisionContext]:
    """Materialize ``_previous`` + ``_verdict`` for body round r≥2 (SPEC §10.3/§11)."""
    prev_rtype = ctx.registry.get(body_primary.type)
    assert prev_rtype is not None
    prev_name = artifact_filename(body_primary.port, prev_rtype)
    last_name = node.body_block_name(len(node.body_chain) - 1)
    prev_src = artifact_path(
        ctx.run_dir / "steps" / node.id / f"{last_name}_r{r - 1}" / "output",
        body_primary.port,
        prev_rtype,
    )

    critic_agent = ctx.agents[node.critic.agent]
    vrtype = ctx.registry.get(_VERDICT_TYPE)
    assert vrtype is not None
    verdict_src = artifact_path(
        ctx.run_dir / "steps" / node.id / f"critic_r{r - 1}" / "output",
        _primary(critic_agent).port,
        vrtype,
    )

    # the FIRST element is the one that revises, so its package supplies the hint
    hint_file = ctx.agent_dir(node.body_chain[0].agent) / "revision_hint.md"
    hint = hint_file.read_text("utf-8") if hint_file.exists() else None

    aux: list[InputSpec] = [
        AuxFileInput(rel_path=f"_previous/{prev_name}", src=prev_src),
        AuxFileInput(rel_path="_verdict/verdict.json", src=verdict_src),
    ]
    revision = RevisionContext(
        previous_path=f"input/_previous/{prev_name}",
        verdict_json=verdict_src.read_text("utf-8"),
        hint=hint,
    )
    return aux, revision


def _verdict_data(
    ctx: MetaContext, node: LoopNode, critic_agent: AgentSpec, r: int
) -> dict[str, object]:
    """The round's verdict artifact, parsed (I4: control comes from this file alone)."""
    vrtype = ctx.registry.get(_VERDICT_TYPE)
    assert vrtype is not None
    path = artifact_path(
        ctx.run_dir / "steps" / node.id / f"critic_r{r}" / "output",
        _primary(critic_agent).port,
        vrtype,
    )
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        # A verdict that cannot be read is not a verdict. Returning empty routes this into
        # the ordinary "not approved" path, where the node fails or the draft passes per
        # `on_max_rounds` — rather than raising out of `run_loop` as an unhandled exception
        # and losing every round already paid for.
        return {}
    return data if isinstance(data, dict) else {}


def _read_verdict(
    ctx: MetaContext, node: LoopNode, critic_agent: AgentSpec, r: int
) -> str:
    """Read the round's verdict from the critic's typed artifact (I4)."""
    return str(_verdict_data(ctx, node, critic_agent, r).get("verdict"))


def _verdict_issues(
    ctx: MetaContext, node: LoopNode, critic_agent: AgentSpec, r: int
) -> list[dict[str, object]]:
    """The round's remarks, as a list (I4). Absent or malformed reads as none."""
    issues = _verdict_data(ctx, node, critic_agent, r).get("issues")
    if not isinstance(issues, list):
        return []
    return [i for i in issues if isinstance(i, dict)]


def _write_unresolved(
    ctx: MetaContext,
    node: LoopNode,
    critic_agent: AgentSpec,
    *,
    chosen: int,
    approved: bool,
) -> int:
    """Write ``steps/<id>/_out/unresolved.md``; returns how many items it names (§10.3).

    What a loop leaves open was the one thing a run never said out loud. A loop that hits
    its ceiling warns "max_rounds reached; passing" and hands the draft on, and everything
    the critic still objected to lives in `steps/<id>/critic_r<n>/output/verdict.json` —
    a path nobody opens who is not already debugging. Measured across two live runs of the
    same article: neither was ever approved, and the remarks that shipped with it were
    real (wrong matrix shapes, a configuration table with wrong dimensions), yet the only
    trace was a warning line in the event log.

    Written by the ENGINE from the typed verdict, not by an agent: it is a transcription of
    a file the engine already has, and putting a model in that path would add a way for the
    record to disagree with what was actually judged.

    A round the critic approved with remarks still gets the file: "publishable, but this is
    wrong" is a finding, and silence about it reads as approval of everything.
    """
    items = _verdict_issues(ctx, node, critic_agent, chosen)
    if not items:
        return 0
    out_dir = ctx.run_dir / "steps" / node.id / "_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    head = (
        f"# Незакрытое: {node.id}, круг {chosen} из {node.params.max_rounds}\n\n"
        + (
            "Критик одобрил черновик и всё же назвал это.\n\n"
            if approved
            else "Круги правки исчерпаны, эти замечания остались открытыми.\n\n"
        )
    )
    lines = []
    for n, item in enumerate(items, 1):
        section = str(item.get("section") or "").strip()
        note = str(item.get("note") or "").strip()
        lines.append(f"{n}. " + (f"[{section}] {note}" if section else note))
    body = head + "\n\n".join(lines) + "\n"
    (out_dir / "unresolved.md").write_text(body, "utf-8")
    return len(items)


def _assemble_loop_output(
    ctx: MetaContext, node: LoopNode, body_agent: AgentSpec, chosen: int
) -> None:
    """Assemble ``steps/<id>/_out/<outName>.<ext>`` from the chosen body round (§10.3)."""
    out_dir = ctx.run_dir / "steps" / node.id / "_out"
    if out_dir.exists():
        shutil.rmtree(long_path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    last_name = node.body_block_name(len(node.body_chain) - 1)
    body_out = ctx.run_dir / "steps" / node.id / f"{last_name}_r{chosen}" / "output"
    body_primary = _primary(body_agent)
    produce_type = {p.port: p.type for p in body_agent.produces}
    for out_name, ref_s in node.outputs.items():
        ref = parse_ref(ref_s)
        src_port = (
            ref.port if isinstance(ref, BodyRef) and ref.port else body_primary.port
        )
        rtype = ctx.registry.get(produce_type.get(src_port, body_primary.type))
        if rtype is not None and rtype.kind.value == "file":
            link_or_copy(
                artifact_path(body_out, src_port, rtype),
                out_dir / artifact_filename(out_name, rtype),
            )
        else:  # dir | any
            dst = out_dir / out_name
            dst.mkdir(parents=True, exist_ok=True)
            src_dir = body_out / src_port
            if src_dir.is_dir():
                for child in sorted(src_dir.iterdir()):
                    link_or_copy(child, dst / child.name)


# --- select (SPEC §10.3) ----------------------------------------------------


async def run_select(node: SelectNode, ctx: MetaContext) -> NodeStatus:
    """Pick one winner from the candidate collection (selector skipped at n=1)."""
    selector_agent = ctx.agents[node.selector.agent]
    model_raw = node.selector.model or node.params.model
    assert model_raw is not None
    model = ctx.resolve_model(model_raw)
    ctx.set_node(node.id, NodeStatus.running)

    cand_ref = parse_ref(node.candidates)
    assert isinstance(cand_ref, DataRef)
    cand_dir = ctx.output_base(cand_ref.node_id) / cand_ref.port
    manifest = CollectionManifest.model_validate(
        json.loads((cand_dir / "_collection.json").read_text("utf-8"))
    )
    ok_slugs = [i.slug for i in manifest.items if i.status is CollectionStatus.ok]
    if not ok_slugs:
        return _fail(ctx, node.id, "no ok candidates")

    if len(ok_slugs) == 1:  # sole candidate: no selector step (SPEC §10.3)
        winner: str | None = ok_slugs[0]
    else:
        coll_ports = [
            p for p in selector_agent.consumes if p.type.startswith("collection<")
        ]
        sp = node.selector.params
        step = await _run_step(
            ctx,
            AgentStepPlan(
                step_id=f"{node.id}.selector",
                node_id=node.id,
                workdir=ctx.run_dir / "steps" / node.id / "selector",
                agent=selector_agent,
                agent_dir=ctx.agent_dir(node.selector.agent),
                model=model,
                registry=ctx.registry,
                inputs=[CollectionInput(port=coll_ports[0].port, src=cand_dir)],
                timeout_s=(
                    (sp.timeout_s if sp else None)
                    or node.params.timeout_s
                    or selector_agent.defaults.timeout_s
                ),
                gate_retries=node.params.gate_retries,
                infra_retries=node.params.infra_retries,
                extra_gate=_winner_gate(ctx, selector_agent, ok_slugs),
            ),
        )
        winner = (
            _read_winner(ctx, node, selector_agent)
            if step.outcome is StepOutcome.ok
            else None
        )
        if winner not in ok_slugs:
            if node.params.fallback == "fail":
                return _fail(ctx, node.id, "selector produced no valid winner")
            winner = ok_slugs[0]  # fallback: first ok by items order (SPEC §10.3)
            _warn(ctx, node.id, f"selector fallback to first ok candidate {winner!r}")

    assert winner is not None
    _assemble_select_output(ctx, node, cand_dir, winner)
    ctx.ledger.set_node_selection(
        node.id, winner=winner, winner_model=_winner_model(ctx, cand_ref, winner)
    )
    ctx.set_node(node.id, NodeStatus.done)
    return NodeStatus.done


def _winner_gate(
    ctx: MetaContext, selector_agent: AgentSpec, ok_slugs: list[str]
) -> Callable[[Path], list[str]]:
    """Extra gate: ``selection.winner`` must be one of the ok slugs (SPEC §10.3)."""
    primary = _primary(selector_agent)
    srtype = ctx.registry.get(_SELECTION_TYPE)

    def check(output_dir: Path) -> list[str]:
        if srtype is None:
            return []
        path = artifact_path(output_dir, primary.port, srtype)
        try:
            winner = json.loads(path.read_text("utf-8")).get("winner")
        except (OSError, json.JSONDecodeError):
            return ["selection: unreadable winner"]
        if winner not in ok_slugs:
            return [f"selection.winner {winner!r} not in ok candidates {ok_slugs}"]
        return []

    return check


def _read_winner(
    ctx: MetaContext, node: SelectNode, selector_agent: AgentSpec
) -> str | None:
    srtype = ctx.registry.get(_SELECTION_TYPE)
    if srtype is None:
        return None
    path = artifact_path(
        ctx.run_dir / "steps" / node.id / "selector" / "output",
        _primary(selector_agent).port,
        srtype,
    )
    try:
        return str(json.loads(path.read_text("utf-8")).get("winner"))
    except (OSError, json.JSONDecodeError):
        return None


def _winner_model(ctx: MetaContext, cand_ref: DataRef, winner: str) -> str | None:
    """Map the winning slug back to its model when candidates come from map_over."""
    map_over = getattr(ctx.nodes.get(cand_ref.node_id), "map_over", None)
    if map_over is None:
        return None
    for m in map_over.models:
        if model_slug(m) == winner:
            return str(m)
    return None


def _assemble_select_output(
    ctx: MetaContext, node: SelectNode, cand_dir: Path, winner: str
) -> None:
    """Assemble ``steps/<id>/_out/out.<ext>`` from the winner element (§10.3)."""
    out_dir = ctx.run_dir / "steps" / node.id / "_out"
    if out_dir.exists():
        shutil.rmtree(long_path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    inner = _select_inner_type(ctx, node)
    rtype = ctx.registry.get(inner) if inner else None
    win_dir = cand_dir / winner
    if rtype is not None and rtype.kind.value == "file":
        payload = _sole_file(win_dir)
        if payload is not None:
            link_or_copy(payload, out_dir / artifact_filename("out", rtype))
    elif win_dir.is_dir():
        # dir/any: the port is a directory ``out/`` so a downstream consumer
        # resolving ``<select>.out`` finds it at ``_out/out/`` (mirror of loop).
        dst = out_dir / "out"
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(win_dir.iterdir()):
            link_or_copy(child, dst / child.name)


def _select_inner_type(ctx: MetaContext, node: SelectNode) -> str | None:
    """Element type X of the candidates ``collection<X>`` (from its manifest)."""
    ref = parse_ref(node.candidates)
    if not isinstance(ref, DataRef):
        return None
    cand_dir = ctx.output_base(ref.node_id) / ref.port
    try:
        t = json.loads((cand_dir / "_collection.json").read_text("utf-8")).get("type")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(t, str):
        return None
    inner, is_coll = parse_type_ref(t)
    return inner if is_coll else t


def _sole_file(d: Path) -> Path | None:
    files = [p for p in sorted(d.iterdir()) if p.is_file()] if d.is_dir() else []
    return files[0] if files else None
