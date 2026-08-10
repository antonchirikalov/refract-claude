"""Project and application configuration formats (SPEC §7).

``project.yaml`` (``ProjectConfig``), ``~/.refract/providers.yaml``
(``ProvidersFile``), ``~/.refract/mcp.yaml`` (``McpFile``).
"""

from __future__ import annotations

from typing import Annotated, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag


class ProjectDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None


class ProjectConfig(BaseModel):
    """``project.yaml`` (SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    version: str
    name: str
    input: str = "./input"
    defaults: ProjectDefaults = Field(default_factory=ProjectDefaults)
    # Capability confirmation policy (SPEC §17 phase 3): a run pauses for a human
    # to approve any listed capability — explicit names in ``confirm`` and/or every
    # capability at/above ``confirm_tier`` (safe|moderate|dangerous) — that an
    # agent needs, before that agent's step runs.
    confirm: list[str] = Field(default_factory=list)
    confirm_tier: str | None = None


class ProviderConfig(BaseModel):
    """One provider entry (SPEC §7). Key = model prefix up to the first ``/``.

    This fork runs on the Claude Code CLI, which authenticates from the subscription
    it is logged into, so ``api_key_env`` is OPTIONAL: a provider without it is
    available when the CLI is present. ``models`` is the catalog of model-ids (or CLI
    aliases such as ``sonnet``/``opus``) offered under this provider — the menu a
    pipeline picks from when assigning ``model:`` per node. ``max_concurrent`` matters
    more here than with a key: a subscription is rate-limited per account, so keep it
    low.
    """

    model_config = ConfigDict(extra="forbid")

    api_key_env: str | None = None
    max_concurrent: int = 2
    models: list[str] = Field(default_factory=list)


class ProvidersFile(BaseModel):
    """``~/.refract/providers.yaml`` (SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    library_path: str | None = None


class McpStdioServer(BaseModel):
    """Stdio MCP server launched via a command (SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)


class McpHttpServer(BaseModel):
    """Remote MCP server reached over HTTP (SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    token_env: str | None = None


def _mcp_discriminator(v: object) -> str:
    if isinstance(v, dict):
        return "http" if "url" in v else "stdio"
    return "http" if getattr(v, "url", None) is not None else "stdio"


McpServer = Annotated[
    Union[
        Annotated[McpStdioServer, Tag("stdio")],
        Annotated[McpHttpServer, Tag("http")],
    ],
    Discriminator(_mcp_discriminator),
]


class McpFile(BaseModel):
    """``~/.refract/mcp.yaml`` (SPEC §7)."""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, McpServer] = Field(default_factory=dict)
