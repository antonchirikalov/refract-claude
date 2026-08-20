"""``pipeline.yaml`` format (SPEC §8).

Node types: ``builtin/<name>``, ``agent``, ``loop``, ``select`` — a
discriminated union keyed by the ``type`` field. Reference strings
(``scan.sources``, ``@body``, ``@choose.winner_model``) are kept verbatim;
parsing/validating them is the graph validator's job (§8.1, §8.3).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator

from refract.models.types import Rule

_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
# An output name becomes a path segment under ``runs/<id>/output/``, so it must not
# be able to traverse out of it or name something the filesystem will refuse.
_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RetryParams(BaseModel):
    """Params inherited by sub-steps of a node (SPEC §8.2)."""

    model_config = ConfigDict(extra="forbid")

    gate_retries: int = 2
    infra_retries: int = 2
    timeout_s: int | None = None  # None → agent defaults → 3600 (resolved later)


class AgentParams(RetryParams):
    """Params for an agent node (SPEC §8.2)."""

    workers: int = 3  # only meaningful with map/map_over (graph enforces)
    on_item_failure: Literal["skip", "fail"] = "skip"
    min_ok: int = 1
    model: str | None = None
    cache: bool = False


class LoopParams(RetryParams):
    """Params for a loop node (SPEC §8.2)."""

    max_rounds: int = 3
    on_max_rounds: Literal["pass", "fail"] = "pass"
    # How many rounds may fail to beat the fewest open items any round has managed before
    # the loop stops buying more. `None` disables the check and spends the whole budget.
    #
    # Two, because one bad round is noise and two in a row is a plateau. Measured on two
    # live runs of one article: 6, 2, 2 open items and 5, 5, 5, 4 — in the second the extra
    # round bought one item for the price of a full round, and no round was ever approved.
    # A strong critic reading eleven thousand characters of technical prose finds about five
    # things every time, so "the critic falls silent" is close to unreachable for that
    # genre and `max_rounds` alone cannot tell a converging loop from a stalled one.
    #
    # Counted on the number of items, not on their text: the same count with different
    # wording is still a loop that is not closing anything, and comparing texts would make
    # a reworded remark look like progress.
    plateau_rounds: int | None = 2
    model: str | None = None
    cache: bool = False


class SelectParams(RetryParams):
    """Params for a select node (SPEC §8.2)."""

    fallback: Literal["first_ok", "fail"] = "first_ok"
    model: str | None = None
    cache: bool = False


class DiscoverParams(RetryParams):
    """Params for a discover node (SPEC §20.1)."""

    min_sources: int = 1
    model: str | None = None
    cache: bool = False


class SubBlockParams(BaseModel):
    """Optional param overrides on a sub-block (``body``/``critic``/``selector``)."""

    model_config = ConfigDict(extra="forbid")

    gate_retries: int | None = None
    infra_retries: int | None = None
    timeout_s: int | None = None
    cache: bool | None = None


class MapOver(BaseModel):
    """``map_over: {models: [...]}`` fan-out over models (SPEC §8.1)."""

    model_config = ConfigDict(extra="forbid")

    models: list[str]


class BodyBlock(BaseModel):
    """``loop.body`` — exactly one agent (SPEC §8)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    model: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    params: SubBlockParams | None = None
    gate_rules: list[Rule] = Field(default_factory=list)


class CriticBlock(BaseModel):
    """``loop.critic`` — exactly one agent producing verdict@v1 (SPEC §8, §10.3)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    model: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    params: SubBlockParams | None = None
    gate_rules: list[Rule] = Field(default_factory=list)


class SelectorBlock(BaseModel):
    """``select.selector`` — one agent producing selection@v1 (SPEC §8, §10.3)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    model: str | None = None
    params: SubBlockParams | None = None


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str

    @field_validator("id")
    @classmethod
    def _node_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"invalid node id: {v!r}")
        return v


class AgentNode(_NodeBase):
    type: Literal["agent"]
    agent: str
    inputs: dict[str, str] = Field(default_factory=dict)
    map: str | None = None
    map_over: MapOver | None = None
    params: AgentParams = Field(default_factory=AgentParams)
    # Extra rules on this node's primary output, on top of the artifact type's own
    # (SPEC §8). Lets a project state its own terms — "this report runs to 45
    # pages" — without pushing one assignment's number into a shared library type.
    gate_rules: list[Rule] = Field(default_factory=list)


class LoopNode(_NodeBase):
    """``type: loop`` — a container: body (one element or a chain) + one critic.

    The chain exists because roles inside a loop are often more than two ("write,
    then fact-check, then judge") while the CONTROL decision stays one verdict. The
    controller is deliberately singular: a container is an archetype = elements +
    one controller + one control type (SPEC §10.3), not a free combinator.
    """

    type: Literal["loop"]
    params: LoopParams = Field(default_factory=LoopParams)
    body: BodyBlock | list[BodyBlock]
    critic: CriticBlock
    outputs: dict[str, str]

    @field_validator("body")
    @classmethod
    def _body_not_empty(
        cls, v: BodyBlock | list[BodyBlock]
    ) -> BodyBlock | list[BodyBlock]:
        if isinstance(v, list) and not v:
            raise ValueError("loop body chain must have at least one element")
        return v

    @property
    def body_chain(self) -> list[BodyBlock]:
        """The body as a chain; a single block is a chain of one."""
        return list(self.body) if isinstance(self.body, list) else [self.body]

    @property
    def body_last(self) -> BodyBlock:
        """The element whose output IS the loop's draft (``@body``)."""
        return self.body_chain[-1]

    def body_block_name(self, index: int) -> str:
        """Ledger/step name of chain element ``index``.

        A single-element body keeps the historical ``body`` (so run dirs, ledger ids
        and the API's block enum are unchanged); a chain numbers from ``body1``.
        """
        return "body" if len(self.body_chain) == 1 else f"body{index + 1}"


class SelectNode(_NodeBase):
    type: Literal["select"]
    candidates: str
    selector: SelectorBlock
    params: SelectParams = Field(default_factory=SelectParams)


class DiscoverNode(_NodeBase):
    """``type: discover`` — network source of ``collection<source@v1>`` (SPEC §20).

    Runs one agent step whose single ``dir`` output the engine then assembles into
    the collection, so the agent never produces a collection itself (I6).
    """

    type: Literal["discover"]
    agent: str
    inputs: dict[str, str] = Field(default_factory=dict)
    params: DiscoverParams = Field(default_factory=DiscoverParams)


class BuiltinNode(_NodeBase):
    """A ``builtin/<name>`` node; ``params`` validated later by the builtin (§13)."""

    type: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _builtin_type(cls, v: str) -> str:
        if not v.startswith("builtin/") or len(v) <= len("builtin/"):
            raise ValueError(f"not a builtin node type: {v!r}")
        return v

    @property
    def builtin_name(self) -> str:
        return self.type.split("/", 1)[1]


def _node_discriminator(v: object) -> str | None:
    t = v.get("type") if isinstance(v, dict) else getattr(v, "type", None)
    if isinstance(t, str):
        if t in ("agent", "loop", "select", "discover"):
            return t
        if t.startswith("builtin/"):
            return "builtin"
    return None


Node = Annotated[
    Union[
        Annotated[AgentNode, Tag("agent")],
        Annotated[LoopNode, Tag("loop")],
        Annotated[SelectNode, Tag("select")],
        Annotated[DiscoverNode, Tag("discover")],
        Annotated[BuiltinNode, Tag("builtin")],
    ],
    Discriminator(_node_discriminator),
]


class Pipeline(BaseModel):
    """``pipeline.yaml`` (SPEC §8)."""

    model_config = ConfigDict(extra="forbid")

    version: str
    name: str
    # What this pipeline expects in the project's input folder (SPEC §8, SPEC-UI §5):
    # ``documents`` — source files to scan; ``brief`` — a single written brief/topic
    # the UI collects as text instead of asking for a folder. Execution is identical
    # (both are just files under input/); this only tells a client what to ask for.
    input_mode: Literal["documents", "brief"] = "documents"
    # Node ids AFTER which the run parks for a human to verify the output (SPEC §21).
    # Pipeline-level rather than a node param: checkpoints apply to every node kind,
    # whose params models differ, and one list is what a client renders.
    checkpoints: list[str] = Field(default_factory=list)
    # What the RUN delivers, as ``{name: "<node>.<port>"}`` (SPEC §22). The name is the
    # filename or directory the artifact takes inside ``runs/<id>/output/``.
    #
    # Declared rather than derived. "Terminal nodes" is the obvious rule and it is wrong
    # for exactly the shape a document conveyor has: the explainer's article is consumed
    # by the illustrator, so the graph calls it non-terminal while it is the deliverable,
    # and the illustrator's directory alone would ship without the text. Declaring it also
    # settles the LAYOUT, which nothing else can: an article carrying
    # ``![](figures/<slug>.png)`` needs that directory called ``figures`` next to it, and
    # only the pipeline knows the promise the artifact made.
    #
    # Empty means the run delivers nothing assembled, which is a fine answer for a
    # pipeline whose result is a directory somebody reads in place.
    outputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[Node]

    @field_validator("outputs")
    @classmethod
    def _output_names(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v:
            if not _OUTPUT_NAME_RE.match(name):
                raise ValueError(
                    f"invalid output name {name!r}: it becomes a filename, so it has to "
                    "be a plain name (letters, digits, dot, dash, underscore)"
                )
        return v
