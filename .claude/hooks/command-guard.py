"""
PreToolUse hook: Block destructive bash commands.

Separate from block-secrets.py — this hook handles blast-radius control
(rm, force-push, system writes, exfiltration) while block-secrets handles
credential protection.

Exit codes:
  0 = allow
  2 = block (stderr shown to Claude as feedback)
"""

import json
import re
import sys

# --- Destructive command patterns ---
DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # File deletion (the "never delete" rule — soft-delete via mv to trash/)
    # Require rm to be followed by a space (catches `rm file`, `rm -rf /`, etc.)
    # but not bare `rm` inside prose like commit messages.
    (re.compile(r"\brm\s+", re.IGNORECASE), "rm command (use mv to trash/ instead)"),
    (re.compile(r"\bunlink\s+", re.IGNORECASE), "unlink command"),
    (re.compile(r"\bshred\s+", re.IGNORECASE), "shred command"),

    # Git destructive operations
    (re.compile(r"\bgit\s+push\s+.*(-f|--force)\b"), "git push --force"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-f"), "git clean -f"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "git branch -D (force delete)"),
    (re.compile(r"\bgit\s+checkout\s+\.\s*$"), "git checkout . (discard all changes)"),
    (re.compile(r"\bgit\s+restore\s+\.\s*$"), "git restore . (discard all changes)"),

    # System-level destructive
    (re.compile(r"\bdd\s+"), "dd (disk write)"),
    (re.compile(r"\bmkfs\b"), "mkfs (format filesystem)"),
    (re.compile(r"\bfdisk\b"), "fdisk (partition editing)"),
]

# System paths that should never be written to
SYSTEM_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:>|tee\s+|cp\s+\S+\s+|mv\s+\S+\s+)(/etc/|/usr/|/var/|/boot/|/sys/|/proc/)"),
]

# Exfiltration endpoints
EXFIL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(
        r"\b(curl|wget)\b.*(-X\s+POST|-X\s+PUT|-d\s|--data\s).*"
        r"(pastebin\.com|requestbin|webhook\.site|pipedream\.net|ngrok\.io)",
        re.IGNORECASE,
    ), "HTTP POST to known exfiltration endpoint"),
]


def check_command(command: str) -> str | None:
    """Check if a bash command is destructive. Returns reason or None."""
    normalized = " ".join(command.split()).strip()

    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if pattern.search(normalized):
            return f"Blocked: {reason}"

    for pattern in SYSTEM_PATH_PATTERNS:
        if pattern.search(normalized):
            return "Blocked: writing to system path"

    for pattern, reason in EXFIL_PATTERNS:
        if pattern.search(normalized):
            return f"Blocked: {reason}"

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("HOOK ERROR (fail-closed): malformed hook input JSON", file=sys.stderr)
        sys.exit(2)

    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    command = hook_input.get("tool_input", {}).get("command", "")
    reason = check_command(command)

    if reason:
        print(
            f"SECURITY: {reason}. "
            "Destructive operations are blocked by policy. "
            "Use mv to Vault/Memory/trash/ for soft-delete.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Fail CLOSED — a crashed security hook must block the tool call,
        # not silently allow it (ratified 2026-07-03).
        print(f"HOOK ERROR (fail-closed): {exc}", file=sys.stderr)
        sys.exit(2)
