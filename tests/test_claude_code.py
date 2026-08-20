"""Tests for the pure parts of the Claude Code adapter (SPEC §12).

``ClaudeCodeRuntime.run_step`` itself is not in the automated suite — it needs the real
CLI and a live subscription (see docs/claude-code-smoke.md). Everything that decides
WHAT gets run and HOW its output is read is pure, and it carries I1/I8/I9, so it is
tested here: the argv, the tool allow-list, the per-step MCP config, the stream-json
parsing and the transient-failure classification.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from refract.cli import AppConfig
from refract.models.config import McpFile, ProvidersFile
from refract.runtime.base import StepSpec
from refract.runtime.claude_code import (
    ClaudeCodeRuntime,
    TurnTrace,
    allowed_tools,
    denied_tools,
    failed_mcp_servers,
    is_transient,
    mcp_config,
    model_alias,
    parse_stream_line,
    unknown_cli_tools,
    unused_mcp_servers,
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


def _spec(
    tmp_path: Path, *, needs: list[str], model: str = "claude/sonnet"
) -> StepSpec:
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
    return ClaudeCodeRuntime(mcp=MCP, exe="claude")


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


def _cmd(tmp_path: Path, *, needs: list[str], mcp: Path | None = None) -> list[str]:
    return _runtime().build_command(
        _spec(tmp_path, needs=needs), mcp, tmp_path / ".system-prompt.md"
    )


class TestCommand:
    def test_carries_system_prompt_file_model_and_format(self, tmp_path: Path) -> None:
        cmd = _cmd(tmp_path, needs=["read", "edit"])
        assert cmd[0] == "claude"
        assert "-p" in cmd
        # bare filename: the CLI resolves it against its cwd, which is the workdir
        assert cmd[cmd.index("--system-prompt-file") + 1] == ".system-prompt.md"
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd  # required for stream-json

    def test_no_prompt_text_on_the_command_line(self, tmp_path: Path) -> None:
        """Windows runs ``claude.cmd``, so cmd.exe re-parses the command line.

        Prompt text carries quotes, newlines and JSON Schema braces; passed as an
        argument it arrives mangled and the flags behind it are lost. Both prompts
        therefore travel out of band — stdin and a file.
        """
        cmd = _cmd(tmp_path, needs=["read", "edit"])
        assert "Task prompt." not in cmd
        assert "You are a source processor." not in cmd
        assert "--system-prompt" not in cmd  # the file variant, not the inline one

    def test_system_prompt_is_written_beside_the_step(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, needs=["read"])
        spec.workdir.mkdir(parents=True, exist_ok=True)
        path = _runtime()._write_system_prompt(spec)
        assert path.parent == spec.workdir
        assert path.read_text(encoding="utf-8") == spec.system_prompt

    def test_never_widens_file_access(self, tmp_path: Path) -> None:
        """I1: the step workdir is the boundary, so --add-dir must never appear."""
        assert "--add-dir" not in _cmd(tmp_path, needs=["read"])

    def test_never_forces_key_auth(self, tmp_path: Path) -> None:
        """--bare would demand an API key; this fork runs on the subscription."""
        assert "--bare" not in _cmd(tmp_path, needs=["read"])

    def test_tools_are_restricted_to_declared_capabilities(
        self, tmp_path: Path
    ) -> None:
        cmd = _cmd(tmp_path, needs=["read"])
        granted = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
        assert "Bash" not in granted and "Write" not in granted

    def test_undeclared_tools_are_denied_not_merely_ungranted(
        self, tmp_path: Path
    ) -> None:
        """--allowedTools alone does not confine the agent.

        Under --permission-mode bypassPermissions every tool is pre-approved, and
        live steps did reach for Bash and ToolSearch without declaring either.
        Removing a tool takes --disallowedTools.
        """
        cmd = _cmd(tmp_path, needs=["read"])
        denied = cmd[cmd.index("--disallowedTools") + 1 :]
        assert "Bash" in denied
        assert "Write" in denied  # 'read' does not grant writing
        assert "Task" in denied  # spawning subagents escapes every other limit
        assert "Read" not in denied  # granted, so not denied

    def test_granting_a_capability_lifts_its_denial(self, tmp_path: Path) -> None:
        cmd = _cmd(tmp_path, needs=["read", "edit", "bash"])
        denied = cmd[cmd.index("--disallowedTools") + 1 :]
        for tool in ("Read", "Glob", "Grep", "Write", "Edit", "Bash"):
            assert tool not in denied
        assert "WebFetch" in denied  # never declared

    def test_mcp_agents_keep_the_tool_that_loads_their_servers(
        self, tmp_path: Path
    ) -> None:
        """A live discover step reached Tavily's tools via ToolSearch."""
        with_mcp = _cmd(tmp_path, needs=["read", "mcp:tavily-remote"])
        assert "ToolSearch" not in with_mcp[with_mcp.index("--disallowedTools") + 1 :]
        without = _cmd(tmp_path, needs=["read"])
        assert "ToolSearch" in without[without.index("--disallowedTools") + 1 :]

    def test_deny_list_and_grant_list_do_not_overlap(self, tmp_path: Path) -> None:
        for needs in (["read"], ["edit"], ["webfetch"], ["read", "edit", "vision"]):
            assert not set(allowed_tools(needs)) & set(denied_tools(needs))

    def test_mcp_config_is_pinned_strictly(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        cmd = _cmd(tmp_path, needs=["mcp:pdf-reader"], mcp=path)
        assert cmd[cmd.index("--mcp-config") + 1] == ".mcp.json"
        # without this the user's own MCP servers would also load (I8)
        assert "--strict-mcp-config" in cmd

    def test_without_mcp_needs_no_config_flags(self, tmp_path: Path) -> None:
        cmd = _cmd(tmp_path, needs=["read"])
        assert "--mcp-config" not in cmd and "--strict-mcp-config" not in cmd


class TestUnusedMcpServers:
    """A live find step never touched Tavily; the trace could not say why."""

    def _trace(self, *tools: str) -> TurnTrace:
        trace = TurnTrace()
        for t in tools:
            trace.events.append({"type": "tool_call", "payload": {"tool": t}})
        return trace

    def test_a_declared_server_that_was_never_called_is_reported(self) -> None:
        trace = self._trace("WebSearch", "WebFetch", "Write")
        assert unused_mcp_servers(["read", "mcp:tavily-remote"], trace) == [
            "tavily-remote"
        ]

    def test_a_server_that_was_used_is_not_reported(self) -> None:
        trace = self._trace("mcp__tavily-remote__tavily_search", "Write")
        assert unused_mcp_servers(["mcp:tavily-remote"], trace) == []

    def test_an_agent_with_no_mcp_needs_reports_nothing(self) -> None:
        assert unused_mcp_servers(["read", "edit"], self._trace("Read")) == []


class TestToolSurfaceDrift:
    """A CLI upgrade can add a tool the deny list has never heard of."""

    def test_a_tool_the_adapter_does_not_know_is_reported(self) -> None:
        assert unknown_cli_tools(["Read", "Bash", "TeleportTool"]) == ["TeleportTool"]

    def test_mcp_tools_are_not_drift(self) -> None:
        # --strict-mcp-config already decides which servers exist for the step
        assert unknown_cli_tools(["Read", "mcp__tavily-remote__tavily_search"]) == []

    def test_a_known_surface_reports_nothing(self) -> None:
        assert unknown_cli_tools(["Read", "Write", "Bash", "Task"]) == []


class TestStreamParsing:
    def _line(self, payload: dict[str, object]) -> str:
        return json.dumps(payload)

    def test_init_event_records_mcp_server_status(self) -> None:
        """A step that got no working server must not look like one that ignored it."""
        trace = TurnTrace()
        parse_stream_line(
            self._line(
                {
                    "type": "system",
                    "subtype": "init",
                    "tools": ["Read"],
                    "mcp_servers": [
                        {"name": "tavily-remote", "status": "connected"},
                        {"name": "pdf-reader", "status": "failed"},
                    ],
                }
            ),
            "s",
            trace,
        )
        recorded = [e for e in trace.events if "mcp_servers" in e.get("payload", {})]
        assert recorded and recorded[0]["payload"]["mcp_servers"] == [
            {"name": "tavily-remote", "status": "connected"},
            {"name": "pdf-reader", "status": "failed"},
        ]

    def test_init_event_records_the_tool_surface(self) -> None:
        trace = TurnTrace()
        parse_stream_line(
            self._line(
                {"type": "system", "subtype": "init", "tools": ["Read", "Bash"]}
            ),
            "s",
            trace,
        )
        assert trace.init_tools == ["Read", "Bash"]

    def test_assistant_text_accumulates(self) -> None:
        trace = TurnTrace()
        for chunk in ("Hello ", "world"):
            parse_stream_line(
                self._line(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": chunk}]},
                    }
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
            self._line(
                {"type": "result", "is_error": True, "result": "usage limit reached"}
            ),
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
        # the exact wording that burned 18 map items in a live run: the subscription
        # says "session limit", which none of the other markers matched
        assert is_transient(
            "You've hit your session limit · resets 5:40pm (Europe/Istanbul)"
        )
        assert is_transient("You've hit your weekly limit")
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
        assert (
            self._app({"claude": {"models": ["sonnet"]}}).available_providers == set()
        )

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


class TestWorkdirGuard:
    """I1 enforced by a hook, not by prose (SPEC §12).

    A live ``find`` step read the working directory out of the CLI's own system
    prompt, retyped the run id with hyphens where the engine had underscores, and
    wrote its entire shelf next to the real run — ``output/`` stayed empty and the
    node was bound to fail its gate two steps from the cause.
    """

    def test_allows_paths_inside_the_workdir(self, tmp_path: Path) -> None:
        from refract.runtime.workdir_guard import offending_path

        wd = tmp_path / "steps" / "find" / "main"
        wd.mkdir(parents=True)
        for inside in ("output/found/a.md", "output", "./output/_index.json"):
            assert offending_path({"file_path": inside}, wd) is None, inside
        assert offending_path({"file_path": str(wd / "output" / "b.md")}, wd) is None

    def test_blocks_the_retyped_run_id_and_absolute_escapes(
        self, tmp_path: Path
    ) -> None:
        from refract.runtime.workdir_guard import offending_path

        runs = tmp_path / "runs"
        wd = runs / "run_20260731_113258" / "steps" / "find" / "main"
        wd.mkdir(parents=True)
        # the exact shape of the live failure: same tree, hyphens for underscores
        retyped = runs / "run-20260731-113258" / "steps" / "find" / "main" / "output"
        assert offending_path({"file_path": str(retyped / "x.md")}, wd) is not None
        assert offending_path({"file_path": "../../../../elsewhere.md"}, wd) is not None
        assert offending_path({"notebook_path": str(tmp_path / "n.ipynb")}, wd)

    def test_hook_exit_code_blocks_and_explains(self, tmp_path: Path) -> None:
        import io

        from refract.runtime import workdir_guard

        wd = tmp_path / "main"
        wd.mkdir()
        payload = json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "o.md")}}
        )
        stdin, sys.stdin = sys.stdin, io.StringIO(payload)
        try:
            code = workdir_guard.main(["guard", str(wd)])
        finally:
            sys.stdin = stdin
        assert code == 2  # 2 = block; stderr reaches the model

    def test_a_malformed_call_never_wedges_the_step(self, tmp_path: Path) -> None:
        import io

        from refract.runtime import workdir_guard

        stdin, sys.stdin = sys.stdin, io.StringIO("not json")
        try:
            assert workdir_guard.main(["guard", str(tmp_path)]) == 0
        finally:
            sys.stdin = stdin

    def test_runtime_writes_settings_and_passes_them_to_the_cli(
        self, tmp_path: Path
    ) -> None:
        from refract.runtime.claude_code import (
            GUARD_FILENAME,
            SETTINGS_FILENAME,
        )

        rt = _runtime()
        spec = _spec(tmp_path, needs=["read", "edit"])
        spec.workdir.mkdir(parents=True, exist_ok=True)
        settings_path = rt._write_workdir_guard(spec)

        assert (spec.workdir / GUARD_FILENAME).exists()  # archived with the attempt
        hook = json.loads(settings_path.read_text("utf-8"))["hooks"]["PreToolUse"][0]
        assert "Write" in hook["matcher"] and "Edit" in hook["matcher"]
        assert str(spec.workdir.resolve()) in hook["hooks"][0]["command"]

        cmd = rt.build_command(
            spec, None, tmp_path / ".system-prompt.md", settings_path
        )
        # bare filename, like every other config file: the CLI resolves it from cwd
        assert cmd[cmd.index("--settings") + 1] == SETTINGS_FILENAME


class TestFailedMcpServers:
    """A declared server that never connects is worse than an unused one (I9).

    A live ``find`` step was granted ``pdf-reader`` precisely because official
    statistics and practice digests are PDFs. The server died on import, its tools
    never appeared, and the step wrote two aspects off as unsupported by sources
    while looking at the documents that supported them — with nothing in the run
    saying the server was down.
    """

    def _trace(self, statuses: dict[str, str]) -> TurnTrace:
        trace = TurnTrace()
        for name, status in statuses.items():
            parse_stream_line(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "tools": [],
                        "mcp_servers": [{"name": name, "status": status}],
                    }
                ),
                "find",
                trace,
            )
        return trace

    def test_connected_server_is_not_reported(self) -> None:
        trace = self._trace({"pdf-reader": "connected"})
        assert failed_mcp_servers(["read", "mcp:pdf-reader"], trace) == []

    def test_a_server_stuck_pending_or_failed_is_reported(self) -> None:
        for status in ("pending", "failed", "needs-auth"):
            trace = self._trace({"pdf-reader": status})
            assert failed_mcp_servers(["mcp:pdf-reader"], trace) == ["pdf-reader"]

    def test_a_server_the_cli_never_mentioned_is_reported(self) -> None:
        assert failed_mcp_servers(["mcp:pdf-reader"], TurnTrace()) == ["pdf-reader"]

    def test_a_later_frame_supersedes_the_init_status(self) -> None:
        trace = TurnTrace()
        for status in ("pending", "connected"):
            parse_stream_line(
                json.dumps(
                    {"type": "system", "mcp_servers": [{"name": "t", "status": status}]}
                ),
                "find",
                trace,
            )
        assert trace.mcp_status["t"] == "connected"
        assert failed_mcp_servers(["mcp:t"], trace) == []

    def test_an_agent_with_no_mcp_needs_reports_nothing(self) -> None:
        assert failed_mcp_servers(["read", "edit"], self._trace({})) == []


def test_every_event_the_adapter_emits_is_a_valid_event() -> None:
    """Adapter telemetry must never fail a step whose gate already passed (SPEC §9).

    A live ``find`` step gathered 19 sources, passed its gate, and was then failed by
    the adapter's own warning: ``'warning' is not a valid EventType``. EventType is
    closed, so anything the adapter emits has to be one of its members.
    """
    from refract.models.ledger import Event, EventType

    emitted: list[dict[str, object]] = []
    trace = TurnTrace()
    parse_stream_line(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Read"],
                "mcp_servers": [{"name": "pdf-reader", "status": "failed"}],
            }
        ),
        "find",
        trace,
    )
    # the shape run_step emits for a server that never connected
    broken = failed_mcp_servers(["mcp:pdf-reader"], trace)
    assert broken == ["pdf-reader"]
    emitted.append(
        {
            "type": "log",
            "step_id": "find",
            "payload": {"warning": f"MCP server(s) never connected: {broken[0]}"},
        }
    )
    for raw in emitted:
        event = Event(seq=1, ts="T", **raw)  # type: ignore[arg-type]
        assert event.type in set(EventType)


class TestEnvPlaceholders:
    """``mcp.yaml`` writes ``{env:VAR}``; the CLI reads ``${VAR}`` (SPEC §7, I8).

    An untranslated placeholder is not an error — it is passed through verbatim. A
    live ``find`` step ran its whole search with the literal string
    ``{env:TAVILY_API_KEY}`` as its API key, got "Invalid Tavily API key" on every
    call, and silently fell back to plain web search.
    """

    def test_stdio_args_are_translated(self) -> None:
        mcp = McpFile.model_validate(
            {
                "servers": {
                    "tavily-remote": {
                        "command": [
                            "npx",
                            "mcp-remote",
                            "https://mcp.tavily.com/mcp/?tavilyApiKey={env:TAVILY_API_KEY}",
                        ],
                        "env": {"EXTRA": "{env:OTHER_KEY}"},
                    }
                }
            }
        )
        entry = mcp_config(["mcp:tavily-remote"], mcp)["mcpServers"]
        assert isinstance(entry, dict)
        server = entry["tavily-remote"]
        assert server["args"][-1].endswith("?tavilyApiKey=${TAVILY_API_KEY}")
        assert server["env"]["EXTRA"] == "${OTHER_KEY}"
        # the secret itself is still never inlined (I8)
        assert "tvly-" not in json.dumps(server)

    def test_text_without_a_placeholder_is_untouched(self) -> None:
        from refract.runtime.claude_code import env_placeholders

        assert env_placeholders("npx") == "npx"
        assert env_placeholders("${ALREADY}") == "${ALREADY}"
        assert env_placeholders("{env:A}/{env:B}") == "${A}/${B}"


# --- I8: the environment a step gets ----------------------------------------


class TestStepEnvironment:
    """The runtime used to spawn the CLI with no `env=`, so a step inherited whatever the
    launching shell held. Measured live: the figure step needed a gateway's variables,
    they were exported into the launching process, and eight other agents in that run got
    a corporate image-provider key they had no business seeing."""

    def _mcp(self) -> McpFile:
        return McpFile.model_validate(
            {
                "servers": {
                    "tavily-remote": {
                        "url": "https://example.org/mcp",
                        "token_env": "TAVILY_API_KEY",
                    },
                    "other": {
                        "command": ["node", "x.js"],
                        "env": {"OTHER_TOKEN": "{env:OTHER_SECRET}"},
                    },
                }
            }
        )

    def test_only_declared_secrets_survive(self) -> None:
        from refract.runtime.claude_code import step_env

        parent = {
            "PATH": "/bin",
            "TAVILY_API_KEY": "t",
            "OTHER_SECRET": "o",
            "MOONSHOT_API_KEY": "leak",
            "SS_GATEWAY_PROFILE": "leak",
            "ANTHROPIC_API_KEY": "k",
        }
        env = step_env(
            parent,
            needs=["read", "mcp:tavily-remote"],
            mcp=self._mcp(),
            provider_key_vars=["ANTHROPIC_API_KEY"],
        )
        assert env["PATH"] == "/bin"
        assert env["TAVILY_API_KEY"] == "t"  # this step declared that server
        assert env["ANTHROPIC_API_KEY"] == "k"  # a declared provider key
        assert "OTHER_SECRET" not in env  # a server this step did not ask for
        assert "MOONSHOT_API_KEY" not in env  # nothing declared it at all
        assert "SS_GATEWAY_PROFILE" not in env

    def test_base_variables_are_kept_because_nothing_runs_without_them(self) -> None:
        from refract.runtime.claude_code import step_env

        parent = {
            "PATH": "/bin",
            "SYSTEMROOT": "C:/Windows",
            "APPDATA": "C:/Users/x/AppData/Roaming",
            "TEMP": "C:/T",
            "USERPROFILE": "C:/Users/x",
            "SECRET": "s",
        }
        env = step_env(parent, needs=[], mcp=McpFile())
        for k in ("PATH", "SYSTEMROOT", "APPDATA", "TEMP", "USERPROFILE"):
            assert k in env, k
        assert "SECRET" not in env

    def test_secret_vars_reads_names_not_values(self) -> None:
        from refract.runtime.claude_code import secret_vars

        assert secret_vars(["mcp:tavily-remote"], self._mcp()) == {"TAVILY_API_KEY"}
        assert secret_vars(["mcp:other"], self._mcp()) == {"OTHER_SECRET"}
        assert secret_vars(["read"], self._mcp()) == set()

    def test_the_escape_hatch_restores_inheritance(self) -> None:
        """A wrong allow-list is found mid-run; the person needs to finish that run."""
        from refract.runtime.claude_code import step_env

        parent = {"PATH": "/bin", "SECRET": "s", "REFRACT_INHERIT_ENV": "1"}
        assert step_env(parent, needs=[], mcp=McpFile()) == parent

    def test_the_hatch_off_by_default_and_by_falsy_values(self) -> None:
        from refract.runtime.claude_code import step_env

        for value in ("", "0", "false", "no"):
            env = step_env(
                {"PATH": "/bin", "SECRET": "s", "REFRACT_INHERIT_ENV": value},
                needs=[],
                mcp=McpFile(),
            )
            assert "SECRET" not in env, value
