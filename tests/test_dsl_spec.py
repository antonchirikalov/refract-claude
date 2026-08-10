"""SPEC-DSL.md must not drift from the language the validator actually enforces.

Two guards, both cheap and both about the same failure mode: a code added to the enum
without a rule written down, or a rule written down for a code that no longer exists.
The third test runs the document's own example through the real validator, so the
worked example in §13 cannot rot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from refract.graph import ValidationContext, load_agents, validate_pipeline
from refract.models.errors import Code
from refract.models.pipeline import Pipeline
from refract.registry import ArtifactRegistry

REPO = Path(__file__).resolve().parents[1]
# The specs are internal and deliberately NOT in the repository (see CLAUDE.md);
# they live under docs/spec/, which .gitignore excludes. On a clone without them
# these guards skip instead of failing — the engine itself is fully tested either way.
DSL_SPEC = REPO / "docs" / "spec" / "SPEC-DSL.md"
LIBRARY = REPO / "library"

pytestmark = pytest.mark.skipif(
    not DSL_SPEC.exists(),
    reason="docs/spec/SPEC-DSL.md is not present in this checkout",
)

_SPEC_TEXT = DSL_SPEC.read_text("utf-8") if DSL_SPEC.exists() else ""
# codes as the document writes them: `E_SOMETHING` / `W_SOMETHING` in backticks
_MENTIONED = set(re.findall(r"`([EW]_[A-Z_]+)`", _SPEC_TEXT))
# codes that own a ROW in the §12 table — the closed list a reader consults. Mention
# anywhere used to satisfy this test, and E_MCP_UNDECLARED slipped through: described
# in §10, absent from the table that claims to be complete.
_TABULATED = set(re.findall(r"^\| `([EW]_[A-Z_]+)`", _SPEC_TEXT, re.M))


def test_every_code_is_documented() -> None:
    missing = sorted(c.value for c in Code if c.value not in _MENTIONED)
    assert missing == [], f"codes missing from SPEC-DSL.md: {missing}"


def test_every_code_has_a_row_in_the_table() -> None:
    missing = sorted(c.value for c in Code if c.value not in _TABULATED)
    assert missing == [], f"codes missing from the §12 table: {missing}"


def test_no_invented_codes() -> None:
    known = {c.value for c in Code}
    invented = sorted(_MENTIONED - known)
    assert invented == [], f"SPEC-DSL.md documents non-existent codes: {invented}"


def _last_yaml_block() -> str:
    blocks = re.findall(r"```yaml\n(.*?)```", _SPEC_TEXT, re.DOTALL)
    assert blocks, "SPEC-DSL.md has no yaml examples"
    return blocks[-1]  # §13: the all-constructs example


def test_spec_example_validates() -> None:
    """The §13 example uses every construct; it must pass the real validator."""
    agents, errors = load_agents(LIBRARY)
    assert errors == [], errors
    pipeline = Pipeline.model_validate(yaml.safe_load(_last_yaml_block()))
    ctx = ValidationContext(
        registry=ArtifactRegistry.load(LIBRARY),
        agents=agents,
        known_providers={"kimi", "openai"},
        available_providers={"kimi", "openai"},
        default_model="kimi/k3",
        known_mcp_servers={"tavily-remote", "pdf-reader", "paperbanana"},
    )
    order, findings = validate_pipeline(pipeline, ctx)
    blocking = [
        (f.code.value, f.node_id, f.message) for f in findings if not f.code.is_warning
    ]
    assert blocking == [], blocking
    # the cross-layer reference in the example must actually order sd_refine last
    assert order.index("refine") < order.index("sd_refine")
