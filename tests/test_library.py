"""Guard tests for the shipped ``library/`` (SPEC §5/§6/§17 migration).

Loads the real registry + agent packages (no MockRuntime needed — pure loading
and graph validation) so a malformed shipped package or type schema fails CI.
"""

from __future__ import annotations

from pathlib import Path

from refract.graph import ValidationContext, load_agents, load_pipeline
from refract.registry import ArtifactRegistry, parse_type_ref

LIBRARY = Path(__file__).resolve().parents[1] / "library"

# The spectra agents migrated as refract-native packages (SPEC §6/§17).
_MIGRATED_AGENTS = {
    "arch_probe@1",
    "arch_critic@1",
    "illustrator@1",
    "confluence_publisher@1",
}


def test_library_agents_load_without_errors() -> None:
    agents, errors = load_agents(LIBRARY)
    assert errors == []
    # the Phase 0 migrated agents (SPEC §17) plus the demo agent are present
    assert {"source_processor@1", "requirements_writer@1", "demo_writer@1"} <= set(
        agents
    )


def test_registry_loads_extract_type_and_schema() -> None:
    reg = ArtifactRegistry.load(LIBRARY)
    # extract@v1 is a json file type whose schema loaded cleanly at registry load
    assert reg.has("extract@v1")
    assert reg.has("requirements@v1")
    assert reg.has("source@v1")


def test_migrated_spectra_agents_present_and_self_consistent() -> None:
    """All 6 migrated agents load cleanly and every port type resolves (§6/§17)."""
    agents, errors = load_agents(LIBRARY)
    assert errors == []
    assert _MIGRATED_AGENTS <= set(agents)

    reg = ArtifactRegistry.load(LIBRARY)
    for ref in _MIGRATED_AGENTS:
        spec = agents[ref]
        ports = list(spec.consumes) + list(spec.produces)
        for port in ports:
            inner, _ = parse_type_ref(port.type)
            assert reg.has(inner) or reg.knows_ref(port.type), (
                ref,
                port.port,
                port.type,
            )
        # I6: exactly one non-optional produce port, never a collection produce.
        non_optional = [p for p in spec.produces if not p.optional]
        assert len(non_optional) == 1, (ref, [p.port for p in non_optional])
        for p in spec.produces:
            _, is_coll = parse_type_ref(p.type)
            assert not is_coll, (ref, p.port)


def test_extract_pipeline_validates(tmp_path: Path) -> None:
    # The canonical Extract shape: scan -> map(source_processor) -> writer.
    pipeline = tmp_path / "extract.yaml"
    pipeline.write_text(
        "\n".join(
            [
                'version: "0.1"',
                "name: extract",
                "nodes:",
                "  - id: scan",
                "    type: builtin/scanner",
                "  - id: extract",
                "    type: agent",
                "    agent: source_processor@1",
                "    map: scan.sources",
                "    params: { workers: 3, min_ok: 1, on_item_failure: skip }",
                "  - id: write",
                "    type: agent",
                "    agent: requirements_writer@1",
                "    inputs: { extracts: extract.extract }",
            ]
        ),
        encoding="utf-8",
    )
    agents, _ = load_agents(LIBRARY)
    ctx = ValidationContext(
        registry=ArtifactRegistry.load(LIBRARY),
        agents=agents,
        known_providers={"kimi"},
        available_providers={"kimi"},
        default_model="kimi/kimi-k3",
    )
    graph = load_pipeline(pipeline, ctx)
    assert graph.ok, [(e.code.value, e.node_id, e.message) for e in graph.errors]
    assert graph.order == ["scan", "extract", "write"]


# An agent whose CRITERIA are tokens of one language, not merely whose output is in it.
# The style critic's rules ARE Russian strings — the calque list, the «ты» forms, the
# quotation marks it replaces; translating them would not generalise the agent, it would
# delete its content. The exemption is per agent and deliberately narrow: everything else
# stays English, because the rule below exists for a real defect.
_LANGUAGE_SPECIFIC_AGENTS = {"article_stylist"}


def test_shipped_prompts_and_specs_are_english() -> None:
    """Prompts are English even when the document they produce is not (§6).

    An agent's output language comes from the brief; hardcoding it in the package
    makes the agent single-assignment. A live run shipped four agents written in
    Ukrainian, which read as if the archetype only did Ukrainian legal research.

    The exception is an agent whose subject IS a language (see the set above): a critic
    of Russian prose cannot state «стоит отметить» in English. Its `agent.yaml`
    description has to say so, so the exemption is visible where the agent is chosen.
    """
    import re

    cyrillic = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
    offenders = []
    for path in sorted((LIBRARY / "agents").glob("*/*.md")):
        if path.parent.name in _LANGUAGE_SPECIFIC_AGENTS:
            continue
        hits = cyrillic.findall(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append((path.parent.name, path.name, len(hits)))
    assert offenders == [], offenders


def test_language_specific_agents_declare_it_in_their_contract() -> None:
    """The exemption above must be legible from the package, not only from this test."""
    for name in _LANGUAGE_SPECIFIC_AGENTS:
        spec = (LIBRARY / "agents" / name / "agent.yaml").read_text(encoding="utf-8")
        assert "Russian" in spec, name
