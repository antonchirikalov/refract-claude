"""The mechanical half of I1: a ``PreToolUse`` hook that confines writes to the step.

``cwd`` = the step workdir is not confinement. The Claude Code CLI states its working
directory in its own system prompt, and a live ``find`` step read that line, retyped the
run id with hyphens where the engine had underscores, and wrote every source it had
gathered to a fabricated absolute path next to the real run — leaving ``output/`` empty
and the node bound to fail its gate two steps from the cause. Nothing in ``--allowedTools``
or ``--permission-mode`` prevents an absolute path, so prose asking the agent to stay put
would be exactly the probabilistic enforcement SPEC forbids for an invariant.

The hook is generated into the step workdir and handed to the CLI via ``--settings``. It
reads the tool call on stdin and exits 2 — block, with the reason on stderr for the model
to read — when the call would touch anything outside the workdir.

Kept dependency-free and executed as a standalone script (not ``-m refract...``) so it
runs under whatever interpreter is on hand and is archived, readable, with the attempt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Tool inputs that name a filesystem path. Bash is not here: it is granted only to
# agents that declare it, and a shell line has no single path field to check — an
# agent that needs a shell is trusted with one.
_PATH_KEYS = ("file_path", "path", "notebook_path", "target_file")


def offending_path(tool_input: dict[str, object], workdir: Path) -> Path | None:
    """The first path in ``tool_input`` that escapes ``workdir``, if any."""
    for key in _PATH_KEYS:
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workdir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - unresolvable path is not ours to judge
            continue
        if resolved != workdir and workdir not in resolved.parents:
            return resolved
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0  # misconfigured hook must not wedge the step
    workdir = Path(argv[1]).resolve()
    try:
        call = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    tool_input = call.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    escaped = offending_path(tool_input, workdir)
    if escaped is None:
        return 0

    print(
        f"Blocked: {escaped} is outside this step's directory. Write only to paths "
        "relative to your working directory — 'output/...' , never an absolute path "
        "and never a path you reconstructed from the working directory line.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main(sys.argv))
