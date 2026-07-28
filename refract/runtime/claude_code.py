"""Claude Code adapter (SPEC §12) — the runtime of this fork.

One ``claude -p`` process per step, launched with ``cwd`` = the step workdir and
WITHOUT ``--add-dir``, so the agent's file access stays inside that directory (I1).
The task prompt goes on the command line, the agent package's ``prompt.md`` goes in
``--system-prompt``, and the capabilities the agent declared in ``needs`` become
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
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from refract.models.config import McpFile, McpHttpServer, McpStdioServer, ProvidersFile
from refract.runtime.base import EventCallback, StepResult, StepSpec

# Windows ships the CLI as a .cmd shim; asyncio needs the exact name.
DEFAULT_EXE = "claude.cmd" if sys.platform == "win32" else "claude"

MCP_CONFIG_FILENAME = ".mcp.json"
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
    "usage limit reached",
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
            tools.append(f"mcp__{need[len('mcp:'):]}")
            continue
        for tool in _CAPABILITY_TOOLS.get(need, ()):
            if tool not in tools:
                tools.append(tool)
    return tools


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
                "args": list(server.command[1:]),
                "env": dict(server.env),
            }
        elif isinstance(server, McpHttpServer):
            entry: dict[str, object] = {"type": "http", "url": server.url}
            if server.token_env:
                # the value is resolved from the run env, never inlined (I8)
                entry["headers"] = {
                    "Authorization": f"Bearer ${{{server.token_env}}}"
                }
            servers[name] = entry
    return {"mcpServers": servers}


@dataclass
class TurnTrace:
    """What the adapter learned from one step's stream-json output."""

    text_parts: list[str] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, object] | None = None
    error: str | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def parse_stream_line(line: str, step_id: str, trace: TurnTrace) -> dict[str, object] | None:
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
    """Runs each step as one ``claude -p`` process (SPEC §12)."""

    def __init__(
        self,
        *,
        providers: ProvidersFile,
        mcp: McpFile,
        exe: str | None = None,
        heartbeat_s: float = 10.0,
        permission_mode: str = "bypassPermissions",
    ) -> None:
        self._providers = providers
        self._mcp = mcp
        self._exe = exe or DEFAULT_EXE
        self._heartbeat_s = heartbeat_s
        self._permission_mode = permission_mode
        self._procs: set[asyncio.subprocess.Process] = set()

    # -- command construction (pure; tested) --

    def build_command(self, spec: StepSpec, mcp_path: Path | None) -> list[str]:
        """The exact argv for this step.

        No ``--add-dir``: the process runs with ``cwd`` = workdir and must not reach
        outside it (I1). No ``--bare``: that would demand an API key instead of the
        subscription this fork runs on.
        """
        cmd = [
            self._exe,
            "-p",
            spec.prompt,
            "--system-prompt",
            spec.system_prompt,
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
        if mcp_path is not None:
            # strict: ignore the user's own MCP configuration entirely (I8)
            cmd += ["--mcp-config", str(mcp_path), "--strict-mcp-config"]
        return cmd

    # -- execution --

    async def run_step(self, spec: StepSpec, on_event: EventCallback) -> StepResult:
        spec.workdir.mkdir(parents=True, exist_ok=True)
        mcp_path = self._write_mcp_config(spec)
        cmd = self.build_command(spec, mcp_path)
        trace = TurnTrace()
        heartbeat: asyncio.Task[None] | None = None
        proc: asyncio.subprocess.Process | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(spec.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._procs.add(proc)
            heartbeat = asyncio.create_task(self._heartbeat(spec, on_event))
            stdout, stderr = await proc.communicate()
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                event = parse_stream_line(line, spec.step_id, trace)
                if event is not None:
                    on_event(event)
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
