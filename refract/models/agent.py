"""Agent package format — ``agent.yaml`` (SPEC §6).

Structural model only: port types are strings (may be ``collection<X>``);
semantic rules that need registry context (single primary output, no
collection produces, HITL shape) are enforced by the graph validator (§8.3).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_PORT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_BASE_CAPABILITIES = frozenset({"read", "edit", "vision", "bash", "webfetch"})

# Capability risk tiers (SPEC §17 phase 3). Ordered safe < moderate < dangerous;
# a project's confirm policy can require human confirmation at/above a tier.
_TIER_ORDER = ("safe", "moderate", "dangerous")
_CAPABILITY_TIER = {
    "read": "safe",
    "vision": "safe",
    "webfetch": "moderate",
    "edit": "moderate",
    "bash": "dangerous",
}


def capability_tier(cap: str) -> str:
    """Risk tier of a capability; ``mcp:<server>`` is moderate, unknown → moderate."""
    if cap.startswith("mcp:"):
        return "moderate"
    return _CAPABILITY_TIER.get(cap, "moderate")


def tier_at_least(cap: str, threshold: str) -> bool:
    """True if ``cap``'s tier is >= ``threshold`` in the safe<moderate<dangerous order."""
    return _TIER_ORDER.index(capability_tier(cap)) >= _TIER_ORDER.index(threshold)


class Port(BaseModel):
    """A consumes/produces port (SPEC §6)."""

    model_config = ConfigDict(extra="forbid")

    port: str
    type: str
    optional: bool = False

    @field_validator("port")
    @classmethod
    def _port_name(cls, v: str) -> str:
        if not _PORT_RE.match(v):
            raise ValueError(f"invalid port name: {v!r}")
        return v


class AgentDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: int = 3600


class AgentSpec(BaseModel):
    """``agent.yaml`` (SPEC §6). Referenced from the graph as ``name@version``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    description: str = ""
    consumes: list[Port] = Field(default_factory=list)
    produces: list[Port]
    needs: list[str] = Field(default_factory=list)
    # Environment variables this agent needs its step to be given (SPEC §6, I8).
    #
    # Only names, never values. An agent that reaches an external service through an MCP
    # server declares it in `needs` and its secret follows from `mcp.yaml`; an agent that
    # shells out to a CLI has nowhere to say what that CLI reads from the environment —
    # and the step environment is an allow-list, so what is not declared is not passed.
    # Measured: the illustrator invokes a figure tool over `bash`, needs four variables,
    # and its failure with none of them present is a stack trace three retries deep.
    #
    # Declaring them also makes the check possible: a missing variable is a validation
    # warning before the run spends anything, not a step that ran without its tool.
    env: list[str] = Field(default_factory=list)
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)

    @field_validator("env")
    @classmethod
    def _env_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _ENV_NAME_RE.match(name):
                raise ValueError(
                    f"invalid environment variable name {name!r}: names only, "
                    "never values"
                )
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"invalid agent name: {v!r}")
        return v

    @field_validator("needs")
    @classmethod
    def _capabilities(cls, v: list[str]) -> list[str]:
        for cap in v:
            if cap in _BASE_CAPABILITIES:
                continue
            if cap.startswith("mcp:") and len(cap) > len("mcp:"):
                continue
            raise ValueError(f"unknown capability: {cap!r}")
        return v

    @property
    def ref(self) -> str:
        """Library reference string, e.g. ``source_processor@1``."""
        return f"{self.name}@{self.version}"
