"""Validation error codes and records (SPEC §8.3).

The closed error-code enum, the structured ``ValidationError`` record used by
the graph validator (collected as a list, never raised one-by-one), and
``RegistryError`` for hard registry-load failures.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class Code(str, Enum):
    """Closed enum of validation codes (SPEC §8.3). ``E_*`` block, ``W_*`` warn."""

    E_YAML = "E_YAML"
    E_SCHEMA = "E_SCHEMA"
    E_DUP_NODE_ID = "E_DUP_NODE_ID"
    E_UNKNOWN_NODE_REF = "E_UNKNOWN_NODE_REF"
    E_UNKNOWN_PORT = "E_UNKNOWN_PORT"
    E_UNKNOWN_AGENT = "E_UNKNOWN_AGENT"
    E_UNKNOWN_TYPE = "E_UNKNOWN_TYPE"
    E_RESERVED_TYPE = "E_RESERVED_TYPE"
    E_TYPE_MISMATCH = "E_TYPE_MISMATCH"
    E_INPUT_MISSING = "E_INPUT_MISSING"
    E_CYCLE = "E_CYCLE"
    E_MODEL_UNRESOLVED = "E_MODEL_UNRESOLVED"
    E_PROVIDER_UNAVAILABLE = "E_PROVIDER_UNAVAILABLE"
    E_MAP_CONFLICT = "E_MAP_CONFLICT"
    E_MAP_PORT_AMBIGUOUS = "E_MAP_PORT_AMBIGUOUS"
    E_NESTED_MAP = "E_NESTED_MAP"
    E_LOOP_SHAPE = "E_LOOP_SHAPE"
    E_BINDING_ILLEGAL = "E_BINDING_ILLEGAL"
    E_AGENT_PRODUCES_COLLECTION = "E_AGENT_PRODUCES_COLLECTION"
    E_HITL_SHAPE = "E_HITL_SHAPE"
    E_DISCOVER_SHAPE = "E_DISCOVER_SHAPE"  # §20.1
    E_GATE_RULES_SHAPE = "E_GATE_RULES_SHAPE"  # §8: node gate_rules on a non-file port
    E_MCP_UNDECLARED = (
        "E_MCP_UNDECLARED"  # §8.3: needs mcp:<server> with no such server
    )
    W_CACHE_UNSUPPORTED = "W_CACHE_UNSUPPORTED"
    W_SECURITY = "W_SECURITY"
    W_THRESHOLDS = "W_THRESHOLDS"

    @property
    def is_warning(self) -> bool:
        return self.value.startswith("W_")


class ValidationError(BaseModel):
    """One structured validation finding (SPEC §8.3)."""

    model_config = ConfigDict(extra="forbid")

    code: Code
    node_id: str | None = None
    message: str = ""


class RegistryError(Exception):
    """Hard failure while loading the artifact type registry (SPEC §5)."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
