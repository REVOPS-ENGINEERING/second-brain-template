#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
if _project_dir:
    sys.path.insert(0, str(Path(_project_dir) / ".claude" / "scripts"))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shared import get_vault_root, log_hook_execution, tail_from_heading, xml_wrap  # noqa: E402

try:
    EASTERN = ZoneInfo(os.environ.get("BRAIN_TIMEZONE", "America/New_York"))
except Exception:
    EASTERN = ZoneInfo("America/New_York")
SOFT_CAP_CHARS = 32_000  # ~8k tokens at 4 chars/token
# Daily-log tail budget. Was a flat 50-line tail, which cut mid-entry — the
# slice routinely opened on a dangling bullet whose `## Session …` header had
# been sliced off, so the boot context asserted orphaned fragments with no
# provenance. Budget in CHARS (entry sizes vary wildly) and then advance to the
# next heading so the slice always opens on a record boundary.
DAILY_TAIL_CHARS = 7_000


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError:
        return None


def _read_stdin_silent() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def main() -> int:
    started = time.monotonic()
    stdin_data = _read_stdin_silent()
    session_id = str(stdin_data.get("session_id") or "unknown")
    source = str(stdin_data.get("source") or "unknown")  # startup | resume | clear | compact

    vault = get_vault_root()

    sections: list[str] = []
    telemetry: list[str] = []  # human-readable section list, e.g. "soul:4231"
    soul_sha = "absent"

    soul = _read_optional(vault / "SOUL.md")
    if soul:
        body = soul.strip()
        sections.append(xml_wrap("soul", body))
        telemetry.append(f"soul:{len(body)}")
        soul_sha = _sha8(body)

    user = _read_optional(vault / "USER.md")
    if user:
        body = user.strip()
        sections.append(xml_wrap("user", body))
        telemetry.append(f"user:{len(body)}")

    # First-run nudge: while the identity files still carry their template
    # placeholders, point the assistant at the setup-assistant skill. The hint
    # self-removes once the placeholders are gone.
    _user_blank = bool(user) and "<!-- Fill this in" in user
    _soul_blank = bool(soul) and "<!-- Replace the placeholder" in soul
    if _user_blank or _soul_blank:
        which = " and ".join(
            n for n, blank in (("USER.md", _user_blank), ("SOUL.md", _soul_blank)) if blank
        )
        sections.append(
            xml_wrap(
                "setup_hint",
                f"{which} still contain template placeholders. Unless the user is "
                "already mid-task, offer to run first-time setup via the "
                "setup-assistant skill (a short interview that fills them in and "
                "health-checks the install).",
            )
        )
        telemetry.append("setup_hint")

    memory = _read_optional(vault / "MEMORY.md")
    if memory:
        body = memory.strip()
        sections.append(xml_wrap("memory", body))
        telemetry.append(f"memory:{len(body)}")

    now = datetime.now(EASTERN)
    # Today only — yesterday's tail is redundant with the 08:00 reflection pass
    # that curates it into MEMORY.md.
    for offset in (0,):
        day = now - timedelta(days=offset)
        date_str = day.strftime("%Y-%m-%d")
        daily = _read_optional(vault / "daily" / f"{date_str}.md")
        if daily:
            tail = tail_from_heading(daily.strip(), DAILY_TAIL_CHARS)
            sections.append(xml_wrap("daily_log", tail, attrs={"date": date_str}))
            telemetry.append(f"daily_log_{date_str}:{len(tail)}")

    body = "\n".join(sections)
    pre_truncate_bytes = len(body)
    truncated = "0"

    if len(body) > SOFT_CAP_CHARS:
        # Trim-priority ladder. Step 1: halve daily-log sections (largest,
        # most recent-heavy). <soul> is never trimmed.
        def _trim_daily(s: str) -> str:
            if not s.startswith("<daily_log"):
                return s
            lines = s.split("\n")
            head, mid, tail = lines[0], lines[1:-1], lines[-1]
            budget = max(5, len(mid) // 2)
            return "\n".join([head, *mid[-budget:], tail])

        sections = [_trim_daily(s) for s in sections]
        body = "\n".join(sections)
        truncated = "daily_trim"
        if len(body) > SOFT_CAP_CHARS:
            # Step 2: hard cut
            truncated = f"{truncated}+hard_cut"
            body = body[:SOFT_CAP_CHARS]

    output = xml_wrap("session_start_context", body)
    sys.stdout.write(output + "\n")
    sys.stdout.flush()

    duration = time.monotonic() - started
    detail = (
        f"source={source} "
        f"sections=[{','.join(telemetry) or 'none'}] "
        f"total_bytes={len(output)} "
        f"pre_trunc={pre_truncate_bytes} "
        f"truncated={truncated} "
        f"soul_sha={soul_sha}"
    )
    log_hook_execution("session_start_context", session_id, "OK", duration, detail)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        try:
            log_hook_execution(
                "session_start_context", "unknown", "ERROR", 0.0, f"crash={type(e).__name__}: {e}"
            )
        except Exception:
            pass
        sys.exit(0)  # never block session start
