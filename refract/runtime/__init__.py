"""AgentRuntime implementations (SPEC §12).

base.py — the AgentRuntime protocol + StepSpec/StepResult; claude_code.py — real
adapter; mock.py — MockRuntime for tests.
"""

from __future__ import annotations

from refract.runtime.base import AgentRuntime, EventCallback, StepResult, StepSpec
from refract.runtime.mock import MockRuntime, ScriptedResponse

__all__ = [
    "AgentRuntime",
    "EventCallback",
    "MockRuntime",
    "ScriptedResponse",
    "StepResult",
    "StepSpec",
]
