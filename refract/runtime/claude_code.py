"""Claude Code adapter (SPEC §12) — the runtime of this fork.

One ``claude -p`` process per step, launched with ``cwd`` = the step workdir and
WITHOUT ``--add-dir``, so the agent's file access stays inside that directory (I1).
The task prompt goes in on stdin, the agent package's ``prompt.md`` goes in
``--system-prompt-file``, and the capabilities the agent declared in ``needs`` become
``--allowedTools``. MCP servers are written to a per-step config and pinned with
``--strict-mcp-config``, so a step sees exactly the servers its agent asked for and
none of the user's own (I8).

No API key is involved: the CLI runs on the Claude subscription it is logged into.
That is the whole point of this fork — ``--bare`` is therefore never passed, since it
would force key-based auth.

Success of the WORK is judged by the gate over ``output/`` (I9/§10.2), not by this
adapter: ``completed=False`` here means an infra failure worth retrying.

Not covered by the automated suite end to end (that needs a real CLI and a real
subscription); the pure parts — command construction and stream-json parsing — are
tested in ``tests/test_claude_code.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from refract.models.config import McpFile, McpHttpServer, McpStdioServer
from refract.runtime.base import EventCallback, StepResult, StepSpec

# Windows ships the CLI as a .cmd shim; asyncio needs the exact name.
DEFAULT_EXE = "claude.cmd" if sys.platform == "win32" else "claude"

MCP_CONFIG_FILENAME = ".mcp.json"
SYSTEM_PROMPT_FILENAME = ".system-prompt.md"
SETTINGS_FILENAME = ".claude-settings.json"
GUARD_FILENAME = ".workdir-guard.py"
TRACE_FILENAME = "agent.events.jsonl"
RAW_FILENAME = "raw.txt"

# refract capability → the Claude Code tools that capability permits (SPEC §6).
# Deliberately narrow: an agent that only declared `read` must not get Write.
_CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    "read": ("Read", "Glob", "Grep"),
    "edit": ("Read", "Write", "Edit"),  # writing an artifact implies reading it back
    "vision": ("Read",),  # images are read with the same tool
    "webfetch": ("WebFetch", "WebSearch"),
    "bash": ("Bash",),
}

# The CLI's own tool surface, as its `system/init` event reports it (Claude Code
# 2.1.x). Granting tools is not the same as confining an agent to them: under
# --permission-mode bypassPermissions every tool is pre-approved and --allowedTools
# only says which need no prompt, so live steps reached for Bash and ToolSearch while
# declaring neither. --disallowedTools is what actually removes a tool, and a deny
# list can only be spelled out against a known surface — hence this constant.
#
# It is maintenance surface: a tool the CLI gains and this list does not know stays
# available to every agent. `unknown_cli_tools()` reports that drift from the init
# event of each run rather than letting it pass silently.
_CLI_TOOLS: tuple[str, ...] = (
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterWorktree",
    "ExitWorktree",
    "Glob",
    "Grep",
    "Monitor",
    "NotebookEdit",
    "PowerShell",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "ShareOnboardingGuide",
    "Skill",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
)

# Provider errors that are worth another attempt rather than a failed step. Subscription
# limits and transport hiccups are transient; a bad request or a missing model is not.
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit",
    "overloaded",
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
    "econnreset",
    # Subscription caps. The CLI words them several ways and only one of them was
    # listed here, so a live map node took "You've hit your session limit · resets
    # 5:40pm" as an agent error and burned all 18 items in seconds — a shelf that
    # cost a full discovery pass, thrown away over a wait.
    "usage limit reached",
    "session limit",
    "weekly limit",
    "limit reached",
    "quota",
)


def model_alias(model: str) -> str:
    """``claude/sonnet`` → ``sonnet``; a bare id passes through unchanged.

    The CLI takes either an alias (``sonnet``, ``opus``, ``haiku``) or a full model id,
    so the engine's ``provider/model`` string needs only its provider stripped.
    """
    _, _, alias = model.partition("/")
    return alias or model


def allowed_tools(needs: list[str]) -> list[str]:
    """Tool names to pass to ``--allowedTools`` for these declared capabilities."""
    tools: list[str] = []
    for need in needs:
        if need.startswith("mcp:"):
            # every tool of that server, e.g. mcp__tavily-remote
            tools.append(f"mcp__{need[len('mcp:') :]}")
            continue
        for tool in _CAPABILITY_TOOLS.get(need, ()):
            if tool not in tools:
                tools.append(tool)
    return tools


def denied_tools(needs: list[str]) -> list[str]:
    """Tool names to pass to ``--disallowedTools``: the CLI surface minus what was granted.

    MCP tools need no entry here — ``--strict-mcp-config`` already means only the
    servers the agent declared are loaded at all.
    """
    granted = set(allowed_tools(needs))
    if any(need.startswith("mcp:") for need in needs):
        # the CLI can defer MCP tool schemas behind ToolSearch — a live discover step
        # loaded Tavily's tools through it, so denying it would deny the server too
        granted.add("ToolSearch")
    return [tool for tool in _CLI_TOOLS if tool not in granted]


def unused_mcp_servers(needs: list[str], trace: "TurnTrace") -> list[str]:
    """Servers the step was given that it never called a tool from.

    Not an error — an agent may simply not need one. It is recorded because the
    alternative reading, that the server failed to start, is invisible otherwise.
    """
    declared = [n[len("mcp:") :] for n in needs if n.startswith("mcp:")]
    if not declared:
        return []
    called: set[str] = set()
    for event in trace.events:
        if event.get("type") != "tool_call":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            called.add(str(payload.get("tool", "")))
    return [s for s in declared if not any(t.startswith(f"mcp__{s}") for t in called)]


def failed_mcp_servers(needs: list[str], trace: "TurnTrace") -> list[str]:
    """Declared servers the CLI never reported as connected.

    Distinct from ``unused_mcp_servers``, and the more serious of the two: the agent
    did not decline the capability, it never had it. A live ``find`` step was granted
    ``pdf-reader`` to open official PDF reports; the server died on import
    (``mcp.server.fastmcp`` gone from a newer ``mcp``), its tools never appeared, and
    the step recorded two whole aspects as unsupported by sources while looking at the
    documents that supported them. Nothing in the run said the server was down.
    """
    declared = [n[len("mcp:") :] for n in needs if n.startswith("mcp:")]
    return [s for s in declared if trace.mcp_status.get(s, "absent") != "connected"]


def unknown_cli_tools(init_tools: list[str]) -> list[str]:
    """CLI tools this adapter has never heard of, so cannot have denied.

    Reported per run instead of ignored: a tool added by a CLI upgrade is available
    to every agent regardless of its declared capabilities until ``_CLI_TOOLS``
    learns about it.
    """
    known = set(_CLI_TOOLS)
    return sorted(
        tool
        for tool in init_tools
        if tool not in known and not tool.startswith("mcp__")
    )


_ENV_PLACEHOLDER = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def env_placeholders(value: str) -> str:
    """``{env:VAR}`` → ``${VAR}``: refract's placeholder in the runtime's dialect.

    ``mcp.yaml`` is refract's own format (SPEC §7) and writes secrets as ``{env:VAR}``
    so no value is ever inlined (I8). The CLI expects its own spelling, ``${VAR}``, and
    an untranslated placeholder is not an error — it is passed through as a literal. A live ``find`` step spent its whole run with
    ``{env:TAVILY_API_KEY}`` as its API key, got "Invalid Tavily API key" on every
    call, and fell back to plain web search without anything in the run saying why.
    """
    return _ENV_PLACEHOLDER.sub(r"${\1}", value)


def mcp_config(needs: list[str], mcp: McpFile) -> dict[str, object]:
    """The ``--mcp-config`` document for a step: only the servers it declared (I8).

    Written in Claude Code's own shape (``mcpServers``), translating refract's two
    server kinds. A server named in ``needs`` but absent from the config is skipped
    here — the graph validator already refuses that pipeline (``E_MCP_UNDECLARED``).
    """
    servers: dict[str, object] = {}
    for need in needs:
        if not need.startswith("mcp:"):
            continue
        name = need[len("mcp:") :]
        server = mcp.servers.get(name)
        if isinstance(server, McpStdioServer):
            servers[name] = {
                "type": "stdio",
                "command": server.command[0],
                "args": [env_placeholders(a) for a in server.command[1:]],
                "env": {k: env_placeholders(v) for k, v in server.env.items()},
            }
        elif isinstance(server, McpHttpServer):
            entry: dict[str, object] = {
                "type": "http",
                "url": env_placeholders(server.url),
            }
            if server.token_env:
                # the value is resolved from the run env, never inlined (I8)
                entry["headers"] = {"Authorization": f"Bearer ${{{server.token_env}}}"}
            servers[name] = entry
    return {"mcpServers": servers}


@dataclass
class TurnTrace:
    """What the adapter learned from one step's stream-json output."""

    text_parts: list[str] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    init_tools: list[str] = field(default_factory=list)
    # last-known state of each MCP server the CLI reported, by name. A server that
    # dies on import is reported here and nowhere else: its tools simply never appear.
    mcp_status: dict[str, str] = field(default_factory=dict)
    usage: dict[str, object] | None = None
    error: str | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def parse_stream_line(
    line: str, step_id: str, trace: TurnTrace
) -> dict[str, object] | None:
    """Absorb one stream-json line; return an event to emit, if any.

    The CLI emits one JSON object per line: ``assistant`` messages (whose content
    blocks carry text and tool_use), a final ``result`` (usage, cost, error flag),
    plus system/user frames we only keep for the trace.
    """
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        # not JSON: keep it in the trace rather than dropping it (I9)
        trace.events.append({"type": "log", "payload": {"raw": line[:500]}})
        return None
    if not isinstance(payload, dict):
        return None

    kind = payload.get("type")
    trace.events.append({"type": "log", "payload": {"stream": kind}})

    # every system frame may carry an updated roster, not just `init`: a server that
    # fails after start-up flips its status in a later frame
    if kind == "system":
        for entry in payload.get("mcp_servers") or []:
            if isinstance(entry, dict) and entry.get("name"):
                trace.mcp_status[str(entry["name"])] = str(entry.get("status", "?"))

    if kind == "system" and payload.get("subtype") == "init":
        tools = payload.get("tools")
        trace.init_tools = [str(t) for t in tools] if isinstance(tools, list) else []
        # Which servers the step actually got, and in what state. Without this a run
        # where an MCP server failed to start reads exactly like a run where the agent
        # simply preferred another tool — telling those apart cost a manual repro (I9).
        servers = payload.get("mcp_servers")
        if isinstance(servers, list):
            trace.events.append(
                {
                    "type": "log",
                    "payload": {
                        "mcp_servers": [
                            {
                                "name": str(s.get("name", "?")),
                                "status": str(s.get("status", "?")),
                            }
                            for s in servers
                            if isinstance(s, dict)
                        ]
                    },
                }
            )

    if kind == "assistant":
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                trace.text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                event: dict[str, object] = {
                    "type": "tool_call",
                    "step_id": step_id,
                    "payload": {"tool": str(block.get("name", "?"))},
                }
                trace.events.append(event)
                return event
        return None

    if kind == "result":
        if payload.get("is_error"):
            trace.error = str(payload.get("result") or payload.get("error") or "error")
        usage = payload.get("usage")
        trace.usage = {
            "cost": payload.get("total_cost_usd") or payload.get("cost_usd"),
            "tokens": usage if isinstance(usage, dict) else None,
            "duration_ms": payload.get("duration_ms"),
        }
    return None


def is_transient(message: str) -> bool:
    """Whether a failure message reads like something a retry could survive."""
    low = message.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


class ClaudeCodeRuntime:
    """Runs each step as one ``claude -p`` process (SPEC §12).

    Takes no provider configuration, and that absence is the point: in this fork the CLI
    authenticates from its own subscription, so there is no key to look up and no model
    catalogue to consult here. Provider data belongs to the parts that use it — the
    scheduler's per-provider concurrency limits and the validator's model resolution.
    The constructor used to accept ``providers`` and store it unread, which promised a
    check this class never performed.
    """

    def __init__(
        self,
        *,
        mcp: McpFile,
        exe: str | None = None,
        heartbeat_s: float = 10.0,
        permission_mode: str = "bypassPermissions",
    ) -> None:
        self._mcp = mcp
        self._exe = exe or DEFAULT_EXE
        self._heartbeat_s = heartbeat_s
        self._permission_mode = permission_mode
        self._procs: set[asyncio.subprocess.Process] = set()

    # -- command construction (pure; tested) --

    def build_command(
        self,
        spec: StepSpec,
        mcp_path: Path | None,
        system_prompt_path: Path,
        settings_path: Path | None = None,
    ) -> list[str]:
        """The exact argv for this step.

        Neither prompt travels on the command line: the task prompt goes in on stdin
        and the system prompt goes in a file. On Windows the CLI is ``claude.cmd``, so
        the command line is re-parsed by ``cmd.exe``; prompts carry quotes, newlines
        and JSON Schema braces, and that round trip mangles them badly enough that the
        agent receives a truncated prompt and the flags after it are lost.

        Both config files are named by their bare filename: the CLI resolves relative
        paths against its own cwd, which IS the workdir they live in, and the engine's
        workdir may itself be relative — spelling the path out would have the CLI join
        it onto the workdir a second time.

        No ``--add-dir``: the process runs with ``cwd`` = workdir and must not reach
        outside it (I1). No ``--bare``: that would demand an API key instead of the
        subscription this fork runs on.
        """
        cmd = [
            self._exe,
            "-p",
            "--system-prompt-file",
            system_prompt_path.name,
            "--model",
            model_alias(spec.model),
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self._permission_mode,
        ]
        tools = allowed_tools(spec.needs)
        if tools:
            cmd += ["--allowedTools", *tools]
        # the half that actually confines the agent: granting is not restricting
        denied = denied_tools(spec.needs)
        if denied:
            cmd += ["--disallowedTools", *denied]
        if mcp_path is not None:
            # strict: ignore the user's own MCP configuration entirely (I8)
            cmd += ["--mcp-config", mcp_path.name, "--strict-mcp-config"]
        if settings_path is not None:
            # the PreToolUse hook that makes I1 mechanical rather than asserted
            cmd += ["--settings", settings_path.name]
        return cmd

    # -- execution --

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        mcp_path = self._write_mcp_config(spec)
        system_prompt_path = self._write_system_prompt(spec)
        settings_path = self._write_workdir_guard(spec)
        cmd = self.build_command(spec, mcp_path, system_prompt_path, settings_path)
        trace = TurnTrace()
        heartbeat: asyncio.Task[None] | None = None
        proc: asyncio.subprocess.Process | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(spec.workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._procs.add(proc)
            heartbeat = asyncio.create_task(self._heartbeat(spec, on_event))
            stdout, stderr = await proc.communicate(spec.prompt.encode("utf-8"))
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                event = parse_stream_line(line, spec.step_id, trace)
                if event is not None:
                    on_event(event)
            unused = unused_mcp_servers(spec.needs, trace)
            if unused:
                # configured, reachable, and never called: the agent chose otherwise.
                # Worth recording — "the server was down" and "the agent preferred
                # WebSearch" look identical in a trace that only lists tool calls.
                trace.events.append(
                    {"type": "log", "payload": {"unused_mcp_servers": unused}}
                )
            broken = failed_mcp_servers(spec.needs, trace)
            if broken:
                # the agent lost a declared capability without being told. Emitted as
                # an event, not just trace-logged: the shape of this failure is a step
                # that looks successful and quietly did less than it was built to do.
                trace.events.append(
                    {"type": "log", "payload": {"failed_mcp_servers": broken}}
                )
                on_event(
                    {
                        # `log`, not a new event kind: EventType is fixed by SPEC §9,
                        # and emitting an unlisted type failed a step whose gate had
                        # already passed. The payload key is what makes it findable.
                        "type": "log",
                        "step_id": spec.step_id,
                        "payload": {
                            "warning": (
                                "MCP server(s) never connected: "
                                + ", ".join(broken)
                                + " — the step ran without capabilities it declared"
                            )
                        },
                    }
                )
            unknown = unknown_cli_tools(trace.init_tools)
            if unknown:
                # not fatal, but it means this step ran with tools nothing denied
                trace.events.append(
                    {"type": "log", "payload": {"undeclared_cli_tools": unknown}}
                )
            self._write_trace(spec, trace, stderr.decode("utf-8", errors="replace"))

            if proc.returncode != 0:
                detail = (
                    trace.error
                    or stderr.decode("utf-8", errors="replace").strip()
                    or f"claude exited with {proc.returncode}"
                )[:500]
                # a limit or a transport failure deserves the engine's infra retries;
                # anything else is the agent's own failure
                return StepResult(
                    completed=not is_transient(detail),
                    agent_error=detail,
                    usage=trace.usage,
                )
            if trace.error:
                return StepResult(
                    completed=not is_transient(trace.error),
                    agent_error=trace.error[:500],
                    usage=trace.usage,
                )
            return StepResult(completed=True, agent_error=None, usage=trace.usage)
        except asyncio.CancelledError:
            # the step timeout (§10.2) cancels us; the trace of the step that hung is
            # exactly the one worth keeping (I9)
            self._write_trace(spec, trace, "[claude: step cancelled by timeout]")
            raise
        except (OSError, ValueError) as exc:
            self._write_trace(spec, trace, f"[claude infra error] {exc}")
            return StepResult(completed=False, agent_error=str(exc)[:500])
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if proc is not None:
                self._procs.discard(proc)
                if proc.returncode is None:
                    proc.kill()

    async def close(self) -> None:
        """Kill anything still running — even on a crash path (SPEC §12)."""
        for proc in list(self._procs):
            if proc.returncode is None:
                proc.kill()
        self._procs.clear()

    # -- helpers --

    async def _heartbeat(self, spec: StepSpec, on_event: EventCallback) -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(self._heartbeat_s)
            elapsed += self._heartbeat_s
            on_event(
                {
                    "type": "heartbeat",
                    "step_id": spec.step_id,
                    "payload": {"elapsed_s": int(elapsed)},
                }
            )

    def _write_mcp_config(self, spec: StepSpec) -> Path | None:
        document = mcp_config(spec.needs, self._mcp)
        servers = document.get("mcpServers")
        if not servers:
            return None
        path = spec.workdir / MCP_CONFIG_FILENAME
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _write_workdir_guard(self, spec: StepSpec) -> Path:
        """Generate the ``PreToolUse`` hook that keeps file writes inside the step (I1).

        ``cwd`` alone does not confine anything: a live ``find`` step read the working
        directory out of the CLI's own system prompt, retyped the run id with hyphens,
        and wrote its whole shelf to a fabricated absolute path — ``output/`` stayed
        empty and the node was set to fail its gate two steps from the cause. The guard
        script is copied in beside the settings file so the attempt archives with the
        exact enforcement it ran under.
        """
        guard = spec.workdir / GUARD_FILENAME
        guard.write_text(
            Path(__file__).with_name("workdir_guard.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                        "hooks": [
                            {
                                "type": "command",
                                # sys.executable, not "python": the CLI inherits this
                                # process's environment but not its venv activation
                                "command": (
                                    f'"{sys.executable}" "{guard.resolve()}" '
                                    f'"{spec.workdir.resolve()}"'
                                ),
                            }
                        ],
                    }
                ]
            }
        }
        path = spec.workdir / SETTINGS_FILENAME
        path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _write_system_prompt(self, spec: StepSpec) -> Path:
        """The agent package's ``prompt.md``, on disk for ``--system-prompt-file``.

        Lives in the step workdir next to ``.mcp.json``, so it is archived with the
        attempt and never reaches beyond the step (I1).
        """
        path = spec.workdir / SYSTEM_PROMPT_FILENAME
        path.write_text(spec.system_prompt, encoding="utf-8")
        return path

    def _write_trace(self, spec: StepSpec, trace: TurnTrace, stderr: str) -> None:
        """Persist ``raw.txt`` + ``agent.events.jsonl`` for this attempt (I9)."""
        raw = trace.text or "[claude: no assistant text]"
        if stderr.strip():
            raw = f"{raw}\n\n--- stderr ---\n{stderr.strip()}"
        (spec.workdir / RAW_FILENAME).write_text(raw, encoding="utf-8")
        lines = [json.dumps(e, ensure_ascii=False) for e in trace.events]
        (spec.workdir / TRACE_FILENAME).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )


def cli_available(exe: str | None = None) -> bool:
    """Whether the Claude Code CLI is on PATH — this fork's notion of provider access.

    There is no API key to check (SPEC §7 CHANGED in this fork): a provider backed by
    the CLI is available when the CLI exists and is logged in. Login state cannot be
    probed without spending a request, so presence is what the validator checks.
    """
    return shutil.which(exe or DEFAULT_EXE) is not None
