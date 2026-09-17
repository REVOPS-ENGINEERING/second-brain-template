# pyright: reportMissingImports=false
"""PreToolUse hook: block Write/Edit on SOUL.md.

Standalone module, NOT wired in `.claude/settings.json`. Imported and passed
through `ClaudeAgentOptions.hooks` by `memory_reflect.py` so the guard is only
active during reflection runs. A regular interactive session retains full SOUL.md
edit access.

Hook signature matches the Claude Agent SDK PreToolUse contract:
    async def hook(input_data, tool_use_id, context) -> dict
Returning `{"decision": "block", "reason": "..."}` refuses the tool call.
Returning `{}` allows it to proceed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def protect_soul_file(
    input_data: Any,
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Block Write/Edit when `file_path`'s basename is exactly `SOUL.md`."""
    tool_input = input_data.get("tool_input") if isinstance(input_data, dict) else None
    if not isinstance(tool_input, dict):
        return {}
    file_path = tool_input.get("file_path", "") or ""
    if not isinstance(file_path, str) or not file_path:
        return {}
    if Path(file_path).name == "SOUL.md":
        return {
            "decision": "block",
            "reason": (
                "Reflection agent cannot modify SOUL.md directly — "
                "write suggestions to today's daily log under "
                "'## Reflection suggests' instead."
            ),
        }
    return {}
