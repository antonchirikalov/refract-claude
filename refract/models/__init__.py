"""Pydantic models for every file format in the spec (SPEC §5–§9).

Formats ARE the models; ad-hoc YAML/JSON parsing is forbidden. Covers the
artifact type registry (§5), agent package (§6), project/app config (§7),
pipeline.yaml (§8), and the run ledger / events (§9).
"""

from __future__ import annotations

from refract.models.agent import AgentDefaults, AgentSpec, Port
from refract.models.config import (
    McpFile,
    McpHttpServer,
    McpServer,
    McpStdioServer,
    ProjectConfig,
    ProjectDefaults,
    ProviderConfig,
    ProvidersFile,
)
from refract.models.errors import Code, RegistryError, ValidationError
from refract.models.ledger import (
    Event,
    EventType,
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    StepOutcome,
    StepState,
    StepStatus,
)
from refract.models.pipeline import (
    AgentNode,
    AgentParams,
    BodyBlock,
    BuiltinNode,
    CriticBlock,
    LoopNode,
    LoopParams,
    MapOver,
    Node,
    Pipeline,
    RetryParams,
    SelectNode,
    SelectorBlock,
    SelectParams,
    SubBlockParams,
)
from refract.models.types import (
    ArtifactTypeDef,
    ArtifactTypesFile,
    CitationClosureRule,
    CollectionItem,
    CollectionManifest,
    CollectionStats,
    CollectionStatus,
    ItemInfo,
    MinLengthRule,
    RegexRule,
    Rule,
    TypeFormat,
    TypeKind,
)

__all__ = [
    # errors
    "Code",
    "RegistryError",
    "ValidationError",
    # types / registry format
    "ArtifactTypeDef",
    "ArtifactTypesFile",
    "CitationClosureRule",
    "CollectionItem",
    "CollectionManifest",
    "CollectionStats",
    "CollectionStatus",
    "ItemInfo",
    "MinLengthRule",
    "RegexRule",
    "Rule",
    "TypeFormat",
    "TypeKind",
    # agent
    "AgentDefaults",
    "AgentSpec",
    "Port",
    # config
    "McpFile",
    "McpHttpServer",
    "McpServer",
    "McpStdioServer",
    "ProjectConfig",
    "ProjectDefaults",
    "ProviderConfig",
    "ProvidersFile",
    # pipeline
    "AgentNode",
    "AgentParams",
    "BodyBlock",
    "BuiltinNode",
    "CriticBlock",
    "LoopNode",
    "LoopParams",
    "MapOver",
    "Node",
    "Pipeline",
    "RetryParams",
    "SelectNode",
    "SelectorBlock",
    "SelectParams",
    "SubBlockParams",
    # ledger / events
    "Event",
    "EventType",
    "NodeState",
    "NodeStatus",
    "RunState",
    "RunStatus",
    "StepOutcome",
    "StepState",
    "StepStatus",
]
