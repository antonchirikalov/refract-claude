"""Graph validation: one test per §8.3 error/warning code, plus LoadedGraph.ok.

SPEC §8.3 closed enum. Every code below is exercised at least once against a
crafted pipeline, with a companion "valid variant does not raise it" check
where practical. E_RESERVED_TYPE is raised by the registry at load time, not
by the graph validator — it is covered in tests/test_registry.py; graph.py
simply relies on an already-loaded registry, so it is not exercised here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from refract.graph import validate_pipeline
from refract.models.errors import Code
from refract.models.pipeline import Pipeline
from tests.graph_fixtures import (
    EXTRACT_PIPELINE_YAML,
    agent_spec,
    make_ctx,
    standard_agents,
)


def _pipeline(yaml_text: str) -> Pipeline:
    return Pipeline.model_validate(yaml.safe_load(yaml_text))


def _codes(errors) -> set[Code]:  # type: ignore[no-untyped-def]
    return {e.code for e in errors}


# --- E_YAML / E_SCHEMA are exercised via parse_pipeline_file (tests/test_graph.py) --
# graph_validation.py exercises the schema-level E_SCHEMA that graph.py itself
# raises (unknown builtin type / invalid builtin params).


class TestESchema:
    def test_invalid_builtin_params_yields_e_schema(self, tmp_path: Path) -> None:
        # SPEC §8.3 / §13 — ScannerParams(extra=forbid) rejects unknown keys
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: { bogus_key: 1 }
"""
        )
        ctx = make_ctx(tmp_path, agents={})
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_SCHEMA in _codes(errors)

    def test_valid_builtin_params_no_e_schema(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: { exclude: [".git"] }
"""
        )
        ctx = make_ctx(tmp_path, agents={})
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_SCHEMA not in _codes(errors)


class TestEDupNodeId:
    def test_duplicate_node_id(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: scan
    type: builtin/scanner
    params: {}
"""
        )
        ctx = make_ctx(tmp_path, agents={})
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_DUP_NODE_ID in _codes(errors)

    def test_unique_ids_no_dup_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_DUP_NODE_ID not in _codes(errors)


class TestEUnknownNodeRef:
    def test_reference_to_unknown_node(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: extract
    type: agent
    agent: source_processor@1
    inputs: { source: nonexistent.sources }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_NODE_REF in _codes(errors)

    def test_known_node_ref_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_NODE_REF not in _codes(errors)


class TestEUnknownPort:
    def test_reference_to_unknown_port(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    inputs: { source: scan.no_such_port }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_PORT in _codes(errors)

    def test_unknown_local_consumes_port(self, tmp_path: Path) -> None:
        # local port name not in agent's consumes
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    inputs: { not_a_real_port: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_PORT in _codes(errors)

    def test_known_port_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_PORT not in _codes(errors)


class TestEUnknownAgent:
    def test_unknown_agent_reference(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: extract
    type: agent
    agent: nonexistent_agent@1
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_AGENT in _codes(errors)

    def test_known_agent_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_AGENT not in _codes(errors)


class TestEUnknownType:
    def test_agent_port_uses_unknown_type(self, tmp_path: Path) -> None:
        agents = {
            "weird_agent@1": agent_spec(
                "weird_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "totally_unknown@v1"}],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: n
    type: agent
    agent: weird_agent@1
    inputs: { source: n.out }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_TYPE in _codes(errors)

    def test_known_types_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_TYPE not in _codes(errors)


class TestETypeMismatch:
    def test_incompatible_edge(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    inputs: { source: extract.extract }
  - id: extract2
    type: agent
    agent: source_processor@1
    inputs: { source: extract.extract }
"""
        )
        # extract.extract is extract@v1, but source_processor.source expects source@v1
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_TYPE_MISMATCH in _codes(errors)

    def test_compatible_edge_no_mismatch(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_TYPE_MISMATCH not in _codes(errors)


class TestEInputMissing:
    def test_non_optional_input_not_connected(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: extract
    type: agent
    agent: source_processor@1
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_INPUT_MISSING in _codes(errors)

    def test_connected_input_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_INPUT_MISSING not in _codes(errors)


class TestECycle:
    def test_self_referencing_cycle(self, tmp_path: Path) -> None:
        agents = {
            "echo_agent@1": agent_spec(
                "echo_agent",
                consumes=[{"port": "inp", "type": "extract@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: a
    type: agent
    agent: echo_agent@1
    inputs: { inp: b.out }
  - id: b
    type: agent
    agent: echo_agent@1
    inputs: { inp: a.out }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_CYCLE in _codes(errors)

    def test_acyclic_graph_no_cycle(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_CYCLE not in _codes(errors)


class TestEModelUnresolved:
    def test_no_model_anywhere_yields_unresolved(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, default_model=None)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MODEL_UNRESOLVED in _codes(errors)

    def test_default_model_resolves(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, default_model="kimi/kimi-k3")
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MODEL_UNRESOLVED not in _codes(errors)


class TestEProviderUnavailable:
    def test_unknown_provider(self, tmp_path: Path) -> None:
        ctx = make_ctx(
            tmp_path,
            known_providers={"kimi"},
            available_providers={"kimi"},
            default_model="mystery/some-model",
        )
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_PROVIDER_UNAVAILABLE in _codes(errors)

    def test_known_but_unavailable_provider(self, tmp_path: Path) -> None:
        # provider is a configured key but its env var is empty (not "available")
        ctx = make_ctx(
            tmp_path,
            known_providers={"kimi"},
            available_providers=set(),
            default_model="kimi/kimi-k3",
        )
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_PROVIDER_UNAVAILABLE in _codes(errors)

    def test_available_provider_no_error(self, tmp_path: Path) -> None:
        ctx = make_ctx(
            tmp_path,
            known_providers={"kimi"},
            available_providers={"kimi"},
            default_model="kimi/kimi-k3",
        )
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_PROVIDER_UNAVAILABLE not in _codes(errors)


class TestEMapConflict:
    def test_map_and_map_over_together(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    map_over: { models: ["kimi/kimi-k3"] }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MAP_CONFLICT in _codes(errors)

    def test_map_alone_no_conflict(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MAP_CONFLICT not in _codes(errors)


class TestEMapPortAmbiguous:
    def test_zero_matching_consumes_ports(self, tmp_path: Path) -> None:
        # source_processor only consumes source@v1; mapping a collection<extract@v1>
        # over it means zero ports of the right type.
        agents = standard_agents()
        agents["extract_collector@1"] = agent_spec(
            "extract_collector",
            consumes=[{"port": "extracts", "type": "collection<extract@v1>"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        )
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
  - id: collect
    type: agent
    agent: extract_collector@1
    map: extract.extract
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MAP_PORT_AMBIGUOUS in _codes(errors)

    def test_two_matching_consumes_ports(self, tmp_path: Path) -> None:
        agents = {
            "ambiguous_agent@1": agent_spec(
                "ambiguous_agent",
                consumes=[
                    {"port": "a", "type": "source@v1"},
                    {"port": "b", "type": "source@v1"},
                ],
                produces=[{"port": "out", "type": "extract@v1"}],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: ambiguous_agent@1
    map: scan.sources
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MAP_PORT_AMBIGUOUS in _codes(errors)

    def test_single_matching_port_no_ambiguity(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_MAP_PORT_AMBIGUOUS not in _codes(errors)


class TestENestedMap:
    def test_map_source_produced_by_map_node(self, tmp_path: Path) -> None:
        agents = standard_agents()
        agents["extract_collector@1"] = agent_spec(
            "extract_collector",
            consumes=[{"port": "extract", "type": "extract@v1"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        )
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
  - id: collect
    type: agent
    agent: extract_collector@1
    map: extract.extract
"""
        )
        # extract.extract is produced by a map-node (extract), so mapping over it
        # again is a nested map (SPEC §16.4)
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_NESTED_MAP in _codes(errors)

    def test_map_source_produced_by_non_map_node_ok(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_NESTED_MAP not in _codes(errors)


class TestELoopShape:
    def test_critic_primary_output_not_verdict(self, tmp_path: Path) -> None:
        agents = standard_agents()
        agents["bad_critic@1"] = agent_spec(
            "bad_critic",
            consumes=[{"port": "doc", "type": "requirements@v1"}],
            produces=[{"port": "out", "type": "requirements@v1"}],
        )
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: refine
    type: loop
    body:   { agent: requirements_writer@1, inputs: { extracts: refine.doc } }
    critic: { agent: bad_critic@1, inputs: { doc: "@body" } }
    outputs: { doc: "@body" }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_LOOP_SHAPE in _codes(errors)

    def test_critic_producing_verdict_ok(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_LOOP_SHAPE not in _codes(errors)

    # --- body chains (SPEC §10.3) ---

    def _chain_agents(self) -> dict:
        agents = standard_agents()
        polisher = agent_spec(
            "polisher",
            consumes=[{"port": "draft", "type": "requirements@v1"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        )
        return {**agents, polisher.ref: polisher}

    def _chain_yaml(self, first_input: str, second_input: str) -> str:
        return f"""
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
  - id: refine
    type: loop
    body:
      - {{ agent: requirements_writer@1, inputs: {{ extracts: {first_input} }} }}
      - {{ agent: polisher@1, inputs: {{ draft: {second_input} }} }}
    critic: {{ agent: requirements_critic@1, inputs: {{ doc: "@body", extracts: extract.extract }} }}
    outputs: {{ doc: "@body" }}
"""

    def test_chain_with_prev_is_valid(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._chain_agents())
        pipeline = _pipeline(self._chain_yaml("extract.extract", '"@prev"'))
        _order, errors = validate_pipeline(pipeline, ctx)
        assert [e for e in errors if not e.code.is_warning] == []

    def test_prev_on_first_element_is_rejected(self, tmp_path: Path) -> None:
        """@prev in the first element points at nothing (SPEC §10.3)."""
        agents = self._chain_agents()
        # first element consuming @prev: legal grammar, impossible position
        yaml_text = self._chain_yaml('"@prev"', '"@prev"')
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(_pipeline(yaml_text), ctx)
        assert Code.E_LOOP_SHAPE in _codes(errors)

    def test_body_element_may_not_reference_body(self, tmp_path: Path) -> None:
        """@body is the body's own output — a body element cannot consume it."""
        ctx = make_ctx(tmp_path, agents=self._chain_agents())
        _order, errors = validate_pipeline(
            _pipeline(self._chain_yaml("extract.extract", '"@body"')), ctx
        )
        assert Code.E_LOOP_SHAPE in _codes(errors)

    def test_internal_ref_outside_a_loop_is_rejected(self, tmp_path: Path) -> None:
        """``@prev``/``@body`` used to be silently ignored on a plain agent node."""
        ctx = make_ctx(tmp_path, agents=self._chain_agents())
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: polish
    type: agent
    agent: polisher@1
    inputs: { draft: "@prev" }
"""
        )
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_LOOP_SHAPE in _codes(errors)

    def test_chain_element_model_is_resolved_per_element(self, tmp_path: Path) -> None:
        """A bad provider on the SECOND element must be reported, not skipped."""
        ctx = make_ctx(tmp_path, agents=self._chain_agents())
        pipeline = _pipeline(
            self._chain_yaml("extract.extract", '"@prev"').replace(
                "{ agent: polisher@1", "{ model: nope/x, agent: polisher@1"
            )
        )
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_PROVIDER_UNAVAILABLE in _codes(errors)


class TestEBindingIllegal:
    def test_scalar_binding_target_not_a_select_node(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: refine
    type: loop
    body:
      agent: requirements_writer@1
      model: "@scan.winner_model"
      inputs: { extracts: refine.doc }
    critic: { agent: requirements_critic@1, inputs: { doc: "@body", extracts: refine.doc } }
    outputs: { doc: "@body" }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_BINDING_ILLEGAL in _codes(errors)

    def test_winner_model_without_map_over_candidates(self, tmp_path: Path) -> None:
        agents = standard_agents()
        agents["selector_agent@1"] = agent_spec(
            "selector_agent",
            consumes=[{"port": "candidates", "type": "collection<extract@v1>"}],
            produces=[{"port": "winner", "type": "selection@v1"}],
        )
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources

  - id: choose
    type: select
    candidates: extract.extract
    selector: { agent: selector_agent@1 }

  - id: refine
    type: loop
    body:
      agent: requirements_writer@1
      model: "@choose.winner_model"
      inputs: { extracts: extract.extract }
    critic: { agent: requirements_critic@1, inputs: { doc: "@body", extracts: extract.extract } }
    outputs: { doc: "@body" }
"""
        )
        # extract.extract came from a plain map, not map_over.models — no
        # winner_model export exists for this select (SPEC §8.1).
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_BINDING_ILLEGAL in _codes(errors)

    def test_valid_map_over_winner_model_binding_ok(self, tmp_path: Path) -> None:
        agents = standard_agents()
        agents["solution_designer@1"] = agent_spec(
            "solution_designer",
            consumes=[{"port": "requirements", "type": "requirements@v1"}],
            produces=[{"port": "design_doc", "type": "design_doc@v1"}],
        )
        agents["solution_design_selector@1"] = agent_spec(
            "solution_design_selector",
            consumes=[{"port": "candidates", "type": "collection<design_doc@v1>"}],
            produces=[{"port": "winner", "type": "selection@v1"}],
        )
        agents["solution_design_critic@1"] = agent_spec(
            "solution_design_critic",
            consumes=[{"port": "doc", "type": "design_doc@v1"}],
            produces=[{"port": "verdict", "type": "verdict@v1"}],
        )
        yaml_text = (
            EXTRACT_PIPELINE_YAML
            + """
  - id: design
    type: agent
    agent: solution_designer@1
    inputs: { requirements: refine.doc }
    map_over: { models: ["kimi/kimi-k3"] }

  - id: choose
    type: select
    candidates: design.design_doc
    selector: { agent: solution_design_selector@1 }

  - id: sd_refine
    type: loop
    params: { max_rounds: 3 }
    body:
      agent: solution_designer@1
      model: "@choose.winner_model"
      inputs: { requirements: refine.doc }
    critic: { agent: solution_design_critic@1, inputs: { doc: "@body" } }
    outputs: { doc: "@body" }
"""
        )
        pipeline = _pipeline(yaml_text)
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_BINDING_ILLEGAL not in _codes(errors)


class TestEAgentProducesCollection:
    def test_agent_produces_collection_type(self, tmp_path: Path) -> None:
        agents = {
            "bad_agent@1": agent_spec(
                "bad_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "collection<extract@v1>"}],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: bad_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_AGENT_PRODUCES_COLLECTION in _codes(errors)

    def test_scalar_produces_ok(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_AGENT_PRODUCES_COLLECTION not in _codes(errors)


class TestEHitlShape:
    def test_more_than_one_optional_port(self, tmp_path: Path) -> None:
        agents = {
            "chatty_agent@1": agent_spec(
                "chatty_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[
                    {"port": "out", "type": "extract@v1"},
                    {"port": "q1", "type": "question@v1", "optional": True},
                    {"port": "q2", "type": "question@v1", "optional": True},
                ],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: chatty_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_HITL_SHAPE in _codes(errors)

    def test_optional_port_wrong_type(self, tmp_path: Path) -> None:
        agents = {
            "chatty_agent@1": agent_spec(
                "chatty_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[
                    {"port": "out", "type": "extract@v1"},
                    {"port": "aux", "type": "extract@v1", "optional": True},
                ],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: chatty_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_HITL_SHAPE in _codes(errors)

    def test_single_question_optional_port_ok(self, tmp_path: Path) -> None:
        agents = {
            "clarifying_agent@1": agent_spec(
                "clarifying_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[
                    {"port": "out", "type": "extract@v1"},
                    {"port": "clarification", "type": "question@v1", "optional": True},
                ],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: clarifying_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_HITL_SHAPE not in _codes(errors)


class TestWCacheUnsupported:
    def test_cache_true_yields_warning(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: { cache: true }
"""
        )
        ctx = make_ctx(tmp_path)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_CACHE_UNSUPPORTED in _codes(errors)
        # warnings never block
        matching = [e for e in errors if e.code == Code.W_CACHE_UNSUPPORTED]
        assert all(e.code.is_warning for e in matching)

    def test_cache_false_no_warning(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_CACHE_UNSUPPORTED not in _codes(errors)


class TestWSecurity:
    def test_risky_capability_reachable_from_scanner_warns(
        self, tmp_path: Path
    ) -> None:
        agents = {
            "bashy_agent@1": agent_spec(
                "bashy_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
                needs=["bash"],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: bashy_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_SECURITY in _codes(errors)

    def test_mcp_capability_reachable_from_scanner_warns(self, tmp_path: Path) -> None:
        agents = {
            "mcp_agent@1": agent_spec(
                "mcp_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
                needs=["mcp:pdf-reader"],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: n
    type: agent
    agent: mcp_agent@1
    inputs: { source: scan.sources }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_SECURITY in _codes(errors)

    def test_not_reachable_from_scanner_no_warning(self, tmp_path: Path) -> None:
        # agent with bash need, but not downstream of any scanner node
        agents = {
            "bashy_agent@1": agent_spec(
                "bashy_agent",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
                needs=["bash"],
            )
        }
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: n
    type: agent
    agent: bashy_agent@1
    inputs: { source: n.out }
"""
        )
        ctx = make_ctx(tmp_path, agents=agents)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_SECURITY not in _codes(errors)

    def test_no_risky_capability_no_warning(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        pipeline = _pipeline(EXTRACT_PIPELINE_YAML)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.W_SECURITY not in _codes(errors)


# --- LoadedGraph.ok -----------------------------------------------------------


class TestLoadedGraphOk:
    def test_ok_false_with_blocking_error(self, tmp_path: Path) -> None:
        from refract.graph import load_pipeline

        p = tmp_path / "pipeline.yaml"
        p.write_text(
            """
version: "0.1"
name: p
nodes:
  - id: extract
    type: agent
    agent: nonexistent_agent@1
""",
            encoding="utf-8",
        )
        ctx = make_ctx(tmp_path)
        loaded = load_pipeline(p, ctx)
        assert loaded.ok is False

    def test_ok_true_with_only_warnings(self, tmp_path: Path) -> None:
        from refract.graph import load_pipeline

        p = tmp_path / "pipeline.yaml"
        p.write_text(
            """
version: "0.1"
name: p
nodes:
  - id: scan
    type: builtin/scanner
    params: {}
  - id: extract
    type: agent
    agent: source_processor@1
    map: scan.sources
    params: { cache: true }
""",
            encoding="utf-8",
        )
        ctx = make_ctx(tmp_path)
        loaded = load_pipeline(p, ctx)
        assert loaded.ok is True
        assert Code.W_CACHE_UNSUPPORTED in _codes(loaded.errors)


class TestEDiscoverShape:
    """SPEC §20.1: a discover agent produces exactly one dir artifact."""

    _AGENTS = {
        "finder@1": agent_spec(
            "finder",
            consumes=[{"port": "brief", "type": "brief@v1"}],
            produces=[{"port": "found", "type": "found_sources@v1"}],
        ),
        "flat_finder@1": agent_spec(
            "flat_finder",
            consumes=[{"port": "brief", "type": "brief@v1"}],
            produces=[{"port": "found", "type": "requirements@v1"}],  # file, not dir
        ),
        "double_finder@1": agent_spec(
            "double_finder",
            consumes=[{"port": "brief", "type": "brief@v1"}],
            produces=[
                {"port": "found", "type": "found_sources@v1"},
                {"port": "extra", "type": "requirements@v1"},
            ],
        ),
        "processor@1": agent_spec(
            "processor",
            consumes=[{"port": "source", "type": "source@v1"}],
            produces=[{"port": "extract", "type": "extract@v1"}],
        ),
    }

    def _yaml(self, agent: str, *, with_map: bool = False) -> str:
        text = f"""
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: find
    type: discover
    agent: {agent}
    inputs: {{ brief: brief.brief }}
"""
        if with_map:
            text += """  - id: extract
    type: agent
    agent: processor@1
    map: find.sources
"""
        return text

    def test_dir_producing_agent_is_accepted(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(_pipeline(self._yaml("finder@1")), ctx)
        assert Code.E_DISCOVER_SHAPE not in _codes(errors)

    def test_non_dir_output_yields_e_discover_shape(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(_pipeline(self._yaml("flat_finder@1")), ctx)
        assert Code.E_DISCOVER_SHAPE in _codes(errors)

    def test_two_primary_ports_yields_e_discover_shape(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(
            _pipeline(self._yaml("double_finder@1")), ctx
        )
        assert Code.E_DISCOVER_SHAPE in _codes(errors)

    def test_unconnected_required_input_yields_e_input_missing(
        self, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        pipeline = _pipeline(
            """
version: "0.1"
name: p
nodes:
  - id: find
    type: discover
    agent: finder@1
"""
        )
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_INPUT_MISSING in _codes(errors)

    def test_map_over_a_discover_collection_is_legal(self, tmp_path: Path) -> None:
        # discover is a SOURCE node like scanner, so mapping its output is allowed —
        # E_NESTED_MAP only bars collections produced by map/map_over (SPEC §20).
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(
            _pipeline(self._yaml("finder@1", with_map=True)), ctx
        )
        assert _codes(errors) - {Code.W_SECURITY} == set()


class TestCheckpoints:
    def test_unknown_checkpoint_node_is_reported(self, tmp_path: Path) -> None:
        # SPEC §21.1: a checkpoint must name a node of this pipeline.
        pipeline = _pipeline(
            """
version: "0.1"
name: p
checkpoints: [nope]
nodes:
  - id: scan
    type: builtin/scanner
"""
        )
        ctx = make_ctx(tmp_path, agents={})
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_NODE_REF in _codes(errors)

    def test_known_checkpoint_node_is_accepted(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
checkpoints: [scan]
nodes:
  - id: scan
    type: builtin/scanner
"""
        )
        ctx = make_ctx(tmp_path, agents={})
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_UNKNOWN_NODE_REF not in _codes(errors)


class TestGateRules:
    """SPEC §8/§5.1: a node may tighten its own gate, but only where rules can run."""

    _AGENTS = {
        "writer@1": agent_spec(
            "writer",
            consumes=[{"port": "brief", "type": "brief@v1"}],
            produces=[{"port": "doc", "type": "requirements@v1"}],
        ),
        "finder@1": agent_spec(
            "finder",
            consumes=[{"port": "brief", "type": "brief@v1"}],
            produces=[{"port": "found", "type": "found_sources@v1"}],
        ),
    }

    def _yaml(self, agent: str) -> str:
        return f"""
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: write
    type: agent
    agent: {agent}
    inputs: {{ brief: brief.brief }}
    gate_rules:
      - {{ rule: min_length, value: 81000 }}
"""

    def test_rules_on_a_file_port_are_accepted(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(_pipeline(self._yaml("writer@1")), ctx)
        assert Code.E_GATE_RULES_SHAPE not in _codes(errors)

    def test_rules_on_a_dir_port_yield_e_gate_rules_shape(self, tmp_path: Path) -> None:
        """Nothing to read on a dir artifact — the rule would promise a guarantee it
        could never check."""
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(_pipeline(self._yaml("finder@1")), ctx)
        assert Code.E_GATE_RULES_SHAPE in _codes(errors)

    def test_no_rules_means_no_complaint(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            """
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: write
    type: agent
    agent: writer@1
    inputs: { brief: brief.brief }
"""
        )
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(pipeline, ctx)
        assert Code.E_GATE_RULES_SHAPE not in _codes(errors)

    def _yaml_rule(self, agent: str, rule: str) -> str:
        return f"""
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: write
    type: agent
    agent: {agent}
    inputs: {{ brief: brief.brief }}
    gate_rules:
      - {rule}
"""

    def test_markdown_rules_on_a_markdown_port_are_accepted(
        self, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        for rule in (
            "{ rule: prose_chars, max: 12000 }",
            "{ rule: no_empty_sections }",
        ):
            _order, errors = validate_pipeline(
                _pipeline(self._yaml_rule("writer@1", rule)), ctx
            )
            assert Code.E_GATE_RULES_SHAPE not in _codes(errors), rule

    def test_markdown_rules_on_a_json_port_yield_e_gate_rules_shape(
        self, tmp_path: Path
    ) -> None:
        """On JSON these rules do not refuse — they LIE: braces count as prose and a
        document with no headings has no empty ones. Emptiness of JSON is its schema's
        question (SPEC-DSL §5.1)."""
        agents = {
            "jsonner@1": agent_spec(
                "jsonner",
                consumes=[{"port": "brief", "type": "brief@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
            )
        }
        ctx = make_ctx(tmp_path, agents=agents)
        for rule in (
            "{ rule: prose_chars, max: 12000 }",
            "{ rule: no_empty_sections }",
        ):
            _order, errors = validate_pipeline(
                _pipeline(self._yaml_rule("jsonner@1", rule)), ctx
            )
            assert Code.E_GATE_RULES_SHAPE in _codes(errors), rule

    def test_forbid_file_that_is_missing_is_caught_before_the_run(
        self, tmp_path: Path
    ) -> None:
        """A gate with no patterns reads exactly like a gate that passed, so the list
        is checked at validation and not only when the step is paid for (SPEC §5)."""
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        _order, errors = validate_pipeline(
            _pipeline(
                self._yaml_rule("writer@1", "{ rule: forbid_file, path: nope.txt }")
            ),
            ctx,
        )
        assert Code.E_FORBID_FILE in _codes(errors)

    def test_forbid_file_that_exists_passes_validation(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        # the test registry's library root is tmp_path itself
        (tmp_path / "bans.txt").write_text("стоит отметить\n", encoding="utf-8")
        _order, errors = validate_pipeline(
            _pipeline(
                self._yaml_rule("writer@1", "{ rule: forbid_file, path: bans.txt }")
            ),
            ctx,
        )
        assert Code.E_FORBID_FILE not in _codes(errors)

    def test_forbid_file_with_no_patterns_is_caught(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._AGENTS)
        (tmp_path / "bans.txt").write_text("# только комментарий\n", encoding="utf-8")
        _order, errors = validate_pipeline(
            _pipeline(
                self._yaml_rule("writer@1", "{ rule: forbid_file, path: bans.txt }")
            ),
            ctx,
        )
        assert Code.E_FORBID_FILE in _codes(errors)


# --- W_THRESHOLDS: a floor that only passes when the finder over-delivers ----


class TestWThresholds:
    """min_ok on a map node against min_sources of the discover node feeding it.

    Two live runs paid for this: one demanded 10 notes from a shelf floored at 8 and
    passed only because the search returned 16; another demanded 12 sources where the
    finder's own anti-duplicate cap stopped it at 8, with every required point covered
    twice over, and the node failed on the threshold.
    """

    def _agents(self):
        return {
            "finder@1": agent_spec(
                "finder",
                consumes=[{"port": "brief", "type": "brief@v1"}],
                produces=[{"port": "found", "type": "found_sources@v1"}],
            ),
            "reader@1": agent_spec(
                "reader",
                consumes=[{"port": "source", "type": "source@v1"}],
                produces=[{"port": "out", "type": "extract@v1"}],
            ),
        }

    def _pipe(self, min_sources: int, min_ok: int):
        return _pipeline(
            f"""
version: "0.1"
name: p
input_mode: brief
nodes:
  - id: brief
    type: builtin/brief
  - id: find
    type: discover
    agent: finder@1
    inputs: {{ brief: brief.brief }}
    params: {{ min_sources: {min_sources} }}
  - id: study
    type: agent
    agent: reader@1
    map: find.sources
    params: {{ min_ok: {min_ok} }}
"""
        )

    def test_min_ok_above_the_floor_warns(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._agents())
        _order, errors = validate_pipeline(self._pipe(8, 10), ctx)
        assert Code.W_THRESHOLDS in _codes(errors)
        message = next(e.message for e in errors if e.code is Code.W_THRESHOLDS)
        assert "min_ok 10 exceeds min_sources 8" in message

    def test_min_ok_equal_to_the_floor_warns_about_zero_tolerance(
        self, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path, agents=self._agents())
        _order, errors = validate_pipeline(self._pipe(6, 6), ctx)
        assert Code.W_THRESHOLDS in _codes(errors)
        assert "one unusable source" in next(
            e.message for e in errors if e.code is Code.W_THRESHOLDS
        )

    def test_min_ok_below_the_floor_is_silent(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, agents=self._agents())
        _order, errors = validate_pipeline(self._pipe(6, 5), ctx)
        assert Code.W_THRESHOLDS not in _codes(errors)

    def test_it_is_a_warning_and_does_not_block_the_run(self, tmp_path: Path) -> None:
        """min_sources is a FLOOR, so a larger min_ok is not provably unsatisfiable —
        it is a bet on the finder over-delivering. A bet is a warning, not an error."""
        ctx = make_ctx(tmp_path, agents=self._agents())
        order, errors = validate_pipeline(self._pipe(8, 10), ctx)
        blocking = [e for e in errors if not e.code.value.startswith("W_")]
        assert blocking == [], blocking
        assert order  # the graph still resolves and would run
