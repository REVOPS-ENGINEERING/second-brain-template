#!/usr/bin/env python3
"""Extract only the conversational content from a Claude Code transcript.

Prints user turns and assistant text from a session .jsonl — NO tool calls,
tool results, thinking blocks, attachments, or sidechain (subagent) records.
This is what a summarizing subagent should read instead of tailing the raw
transcript: raw .jsonl lines are dominated by tool payloads that waste tokens
and can contain credentials echoed by tools.

Output is capped (default 20000 chars); when over the cap, the EARLIEST turns
are dropped so the end of the session — where the project state landed — is
always kept.

Usage:
    extract_transcript.py FILE.jsonl [--max-chars N]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_MAX_CHARS = 20000


def text_of(content) -> str:
    """Plain text of a message's content; tool blocks contribute nothing."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def main() -> None:
    args = sys.argv[1:]
    max_chars = DEFAULT_MAX_CHARS
    if "--max-chars" in args:
        i = args.index("--max-chars")
        max_chars = int(args[i + 1])
        del args[i:i + 2]
    if not args:
        print("usage: extract_transcript.py FILE.jsonl [--max-chars N]",
              file=sys.stderr)
        sys.exit(2)
    path = Path(args[0])

    turns: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            if obj.get("isSidechain") or obj.get("isMeta"):
                continue
            text = text_of(obj.get("message", {}).get("content"))
            if not text:
                continue  # e.g. user records that only carry tool_results
            label = "USER" if obj["type"] == "user" else "ASSISTANT"
            turns.append(f"{label}:\n{text}")

    # Keep the tail: drop earliest turns until under the cap.
    total = sum(len(t) + 2 for t in turns)
    dropped = 0
    while turns and total > max_chars:
        total -= len(turns[0]) + 2
        turns.pop(0)
        dropped += 1

    if dropped:
        print(f"[... {dropped} earlier turns truncated to fit "
              f"{max_chars} chars ...]\n")
    print("\n\n".join(turns))


if __name__ == "__main__":
    main()
