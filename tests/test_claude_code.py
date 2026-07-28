"""Tests for the pure parts of the Claude Code adapter (SPEC §12).

``ClaudeCodeRuntime.run_step`` itself is not in the automated suite — it needs the real
CLI and a live subscription (see docs/claude-code-smoke.md). Everything that decides
WHAT gets run and HOW its output is read is pure, and it carries I1/I8/I9, so it is
tested here: the argv, the tool allow-list, the per-step MCP config, the stream-json
parsing and the transient-failure classification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from refract.cli import AppConfig
from refract.models.config import McpFile, ProvidersFile
from refract.runtime.base import StepSpec
from refract.runtime.claude_code import (
    ClaudeCodeRuntime,
    TurnTrace,
    allowed_tools,
    is_transient,
    mcp_config,
    model_alias,
    parse_stream_line,
)

MCP = McpFile.model_validate(
    {
        "servers": {
            "pdf-reader": {"command": ["npx", "-y", "@mcp/pdf-reader"], "env": {}},
            "tavily-remote": {
                "url": "https://mcp.tavily.com/mcp/",
                "token_env": "TAVILY_API_KEY",
            },
            "unused": {"command": ["true"]},
        }
    }
)


def _spec(tmp_path: Path, *, needs: list[str], model: str = "claude/sonnet") -> StepSpec:
    return StepSpec(
        step_id="extract:rfp-md",
        agent_dir=tmp_path / "agent",
        model=model,
        workdir=tmp_path / "work",
        prompt="Task prompt.",
        system_prompt="You are a source processor.",
        needs=needs,
        timeout_s=1800,
    )


def _runtime() -> ClaudeCodeRuntime:
    return ClaudeCodeRuntime(providers=ProvidersFile(), mcp=MCP, exe="claude")


class TestModelAlias:
    def test_strips_the_provider(self) -> None:
        assert model_alias("claude/sonnet") == "sonnet"
        assert model_alias("claude/claude-opus-5") == "claude-opus-5"

    def test_a_bare_alias_passes_through(self) -> None:
        assert model_alias("opus") == "opus"


class TestAllowedTools:
    def test_capabilities_map_to_narrow_tool_sets(self) -> None:
        assert allowed_tools(["read"]) == ["Read", "Glob", "Grep"]
        # an agent that only reads must NOT receive Write
        assert "Write" not in allowed_tools(["read", "vision"])
        assert allowed_tools(["bash"]) == ["Bash"]

    def test_edit_implies_reading_back_and_does_not_duplicate(self) -> None:
        tools = allowed_tools(["read", "edit"])
        assert tools.count("Read") == 1
        assert {"Write", "Edit"} <= set(tools)

    def test_mcp_server_becomes_its_tool_prefix(self) -> None:
        assert allowed_tools(["mcp:pdf-reader"]) == ["mcp__pdf-reader"]


class TestMcpConfig:
    def test_only_declared_servers_are_included(self) -> None:
        doc = mcp_config(["read", "mcp:pdf-reader"], MCP)
        servers = doc["mcpServers"]
        assert isinstance(servers, dict)
        # I8: the user's other servers must not leak into the step
        assert set(servers) == {"pdf-reader"}
        assert servers["pdf-reader"]["command"] == "npx"
        assert servers["pdf-reader"]["args"] == ["-y", "@mcp/pdf-reader"]

    def test_remote_server_passes_a_token_placeholder_not_a_value(self) -> None:
        servers = mcp_config(["mcp:tavily-remote"], MCP)["mcpServers"]
        assert isinstance(servers, dict)
        entry = servers["tavily-remote"]
        assert entry["url"] == "https://mcp.tavily.com/mcp/"
        # the secret itself never appears — only the variable to resolve (I8)
        assert entry["headers"]["Authorization"] == "Bearer ${TAVILY_API_KEY}"

    def test_no_mcp_needs_means_no_servers(self) -> None:
        assert mcp_config(["read", "edit"], MCP)["mcpServers"] == {}


class TestCommand:
    def test_carries_prompt_system_prompt_model_and_format(self, tmp_path: Path) -> None:
        cmd = _runtime().build_command(_spec(tmp_path, needs=["read", "edit"]), None)
        assert cmd[0] == "claude"
        assert "-p" in cmd and "Task prompt." in cmd
        assert cmd[cmd.index("--system-prompt") + 1] == "You are a source processor."
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd  # required for stream-json

    def test_never_widens_file_access(self, tmp_path: Path) -> None:
        """I1: the step workdir is the boundary, so --add-dir must never appear."""
        cmd = _runtime().build_command(_spec(tmp_path, needs=["read"]), None)
        assert "--add-dir" not in cmd

    def test_never_forces_key_auth(self, tmp_path: Path) -> None:
        """--bare would demand an API key; this fork runs on the subscription."""
        cmd = _runtime().build_command(_spec(tmp_path, needs=["read"]), None)
        assert "--bare" not in cmd

    def test_tools_are_restricted_to_declared_capabilities(self, tmp_path: Path) -> None:
        cmd = _runtime().build_command(_spec(tmp_path, needs=["read"]), None)
        tail = cmd[cmd.index("--allowedTools") + 1 :]
        assert "Bash" not in tail and "Write" not in tail

    def test_mcp_config_is_pinned_strictly(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        cmd = _runtime().build_command(_spec(tmp_path, needs=["mcp:pdf-reader"]), path)
        assert cmd[cmd.index("--mcp-config") + 1] == str(path)
        # without this the user's own MCP servers would also load (I8)
        assert "--strict-mcp-config" in cmd

    def test_without_mcp_needs_no_config_flags(self, tmp_path: Path) -> None:
        cmd = _runtime().build_command(_spec(tmp_path, needs=["read"]), None)
        assert "--mcp-config" not in cmd and "--strict-mcp-config" not in cmd


class TestStreamParsing:
    def _line(self, payload: dict[str, object]) -> str:
        return json.dumps(payload)

    def test_assistant_text_accumulates(self) -> None:
        trace = TurnTrace()
        for chunk in ("Hello ", "world"):
            parse_stream_line(
                self._line(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": chunk}]}}
                ),
                "s",
                trace,
            )
        assert trace.text == "Hello world"

    def test_tool_use_becomes_an_event(self) -> None:
        trace = TurnTrace()
        event = parse_stream_line(
            self._line(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Read"}]},
                }
            ),
            "extract:rfp-md",
            trace,
        )
        assert event == {
            "type": "tool_call",
            "step_id": "extract:rfp-md",
            "payload": {"tool": "Read"},
        }

    def test_result_carries_usage(self) -> None:
        trace = TurnTrace()
        parse_stream_line(
            self._line(
                {
                    "type": "result",
                    "total_cost_usd": 0.42,
                    "duration_ms": 1234,
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
            ),
            "s",
            trace,
        )
        assert trace.usage is not None
        assert trace.usage["cost"] == 0.42
        assert trace.usage["duration_ms"] == 1234
        assert trace.error is None

    def test_error_result_is_captured(self) -> None:
        trace = TurnTrace()
        parse_stream_line(
            self._line({"type": "result", "is_error": True, "result": "usage limit reached"}),
            "s",
            trace,
        )
        assert trace.error == "usage limit reached"

    def test_garbage_is_kept_in_the_trace_not_dropped(self) -> None:
        """I9: whatever the CLI printed must be recoverable afterwards."""
        trace = TurnTrace()
        assert parse_stream_line("not json at all", "s", trace) is None
        assert any("raw" in str(e.get("payload")) for e in trace.events)

    def test_blank_lines_are_ignored(self) -> None:
        trace = TurnTrace()
        assert parse_stream_line("   ", "s", trace) is None
        assert trace.events == []


class TestTransientClassification:
    def test_limits_and_transport_are_retryable(self) -> None:
        assert is_transient("Usage limit reached — resets at 4pm")
        assert is_transient("API Error 429: rate_limit_error")
        assert is_transient("Overloaded")
        assert is_transient("ECONNRESET while connecting")

    def test_real_failures_are_not(self) -> None:
        assert not is_transient("No such model: sonnet-9")
        assert not is_transient("invalid request: prompt too long")
        assert not is_transient("permission denied for tool Bash")


class TestProviderAvailability:
    """A CLI-backed provider has no key to check (SPEC §7, CHANGED in this fork)."""

    def _app(self, providers: dict[str, object]) -> AppConfig:
        return AppConfig(
            library_path=Path("library"),
            providers=ProvidersFile.model_validate({"providers": providers}),
        )

    def test_keyless_provider_is_available_when_the_cli_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "refract.runtime.claude_code.cli_available", lambda exe=None: True
        )
        assert self._app({"claude": {"models": ["sonnet"]}}).available_providers == {
            "claude"
        }

    def test_keyless_provider_is_unavailable_without_the_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "refract.runtime.claude_code.cli_available", lambda exe=None: False
        )
        assert self._app({"claude": {"models": ["sonnet"]}}).available_providers == set()

    def test_a_provider_with_a_key_still_goes_by_its_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_KEY", "x")
        monkeypatch.setattr(
            "refract.runtime.claude_code.cli_available", lambda exe=None: False
        )
        app = self._app({"withkey": {"api_key_env": "SOME_KEY", "models": ["m"]}})
        assert app.available_providers == {"withkey"}

    def test_an_empty_key_variable_means_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_KEY", "   ")
        app = self._app({"withkey": {"api_key_env": "SOME_KEY", "models": ["m"]}})
        assert app.available_providers == set()
