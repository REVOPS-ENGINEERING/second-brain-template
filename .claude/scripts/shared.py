from __future__ import annotations

import fcntl
import os
import random
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

def _load_timezone() -> ZoneInfo:
    """Timezone for daily-log dates and reflection windows.

    Override with BRAIN_TIMEZONE (any IANA name, e.g. Europe/London).
    Falls back to America/New_York on a bad/missing value.
    """
    name = os.environ.get("BRAIN_TIMEZONE", "America/New_York")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/New_York")


# Name kept as EASTERN so every call site stays unchanged.
EASTERN = _load_timezone()


def get_project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "CLAUDE.md").is_file():
            return parent
    raise RuntimeError("CLAUDE.md not found walking up from shared.py")


def get_vault_root() -> Path:
    env = os.environ.get("BRAIN_VAULT_ROOT")
    if env:
        path = Path(env).resolve()
        # Trust-the-caller, but warn loudly on misconfiguration. Silent
        # fallthrough to an empty/missing vault was the original failure mode
        # — preflight would report memory_bytes=0 and the agent would run
        # against a phantom vault. A logged warning makes the misconfig visible
        # without raising (graceful degradation matches the rest of the codebase).
        if not path.exists() or not (path / "MEMORY.md").exists():
            import logging
            logging.getLogger("brain.shared").warning(
                "BRAIN_VAULT_ROOT=%s does not exist or has no MEMORY.md — "
                "callers will see an empty vault.", path,
            )
        return path
    return get_project_root() / "Vault" / "Memory"


def is_invoked_by_agent() -> bool:
    return bool(os.environ.get("CLAUDE_INVOKED_BY"))


@contextmanager
def file_lock(path: str | Path, *, timeout: float | None = None):
    # Lock on a sibling .lock file so readers of `path` don't compete with writers.
    # When timeout is None, block indefinitely (backward-compat default).
    # When timeout is set, poll LOCK_NB until the deadline then raise TimeoutError.
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        if timeout is None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Could not acquire lock on {lock_path} within {timeout}s"
                        )
                    time.sleep(0.1)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def atomic_write(path: str | Path, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{p}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _status_from_exc(exc: BaseException) -> int | None:
    for attr in ("status", "status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        if isinstance(val, int):
            return val
    return None


def _retry_after_from_exc(exc: BaseException) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        val = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def with_retry(
    fn: Callable[[], Any],
    max_tries: int = 5,
    retry_on: Iterable[int] = (429, 500, 502, 503, 504),
    base_delay: float = 0.5,
) -> Any:
    retry_codes = set(retry_on)
    last_exc: BaseException | None = None
    for attempt in range(max_tries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            status = _status_from_exc(exc)
            if status is not None and status not in retry_codes:
                raise
            if attempt == max_tries - 1:
                raise
            retry_after = _retry_after_from_exc(exc)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_retry exhausted without exception")


def append_to_daily_log(body: str, section_header: str | None = None) -> Path:
    vault = get_vault_root()
    now = datetime.now(EASTERN)
    date_str = now.strftime("%Y-%m-%d")
    daily_dir = vault / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    path = daily_dir / f"{date_str}.md"

    with file_lock(path):
        if not path.exists():
            initial = (
                f"---\n"
                f"date: {date_str}\n"
                f"tags: [daily]\n"
                f"---\n\n"
                f"# {date_str}\n"
            )
            path.write_text(initial, encoding="utf-8")
        header = section_header or now.strftime("%H:%M")
        chunk = f"\n## {header}\n{body.rstrip()}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(chunk)
    return path


# ---------- Flush / reflection body normalization ----------
#
# Both background writers (memory_flush, memory_reflect) append raw model output
# to the append-only daily logs, which SessionStart re-injects at every boot —
# so emergent model junk becomes permanent memory. Two shapes leak:
#
#   flush      the summarizer emits the `FLUSH_OK` "nothing to save" sentinel,
#              then reverses itself ("Wait — there are a few things worth
#              capturing…") and writes real content after it.
#   reflection the inter-tool narration ("Now let me read MEMORY.md…") lands
#              ahead of the mandated `Δ bytes:` summary, glued to it with no
#              newline (memory_reflect's prompt mandates the lead token).
#
# These are PURE functions (stdlib `re` only) so tests import them without the
# SDK, and — unlike the I/O helpers below — they do NOT swallow exceptions.
# Callers must treat a `None` return as "skip the write and WARN", never crash.

import re  # noqa: E402

# Where real content starts after a reversal preamble. Three emitted shapes:
# an ATX heading (`## DECISIONS`), a bold run-in (`**DECISIONS**`), and a
# bulleted bold label (`- **FACTS**`). The third arm is load-bearing: the
# model does emit it, and without it the cleanup sweep deletes real content.
# Do NOT narrow.
_HEADING_RE = re.compile(r"^(#{1,6}\s|\*\*\w|[-*]\s+\*\*\w)")
_FLUSH_OK_RE = re.compile(r"^\s*flush_ok\s*$", re.IGNORECASE)
# The reversal sometimes lands on the SAME line as the sentinel
# (`FLUSH_OK — wait, there's substantive content here.`), so the whole-line
# regex above never sees it and the block leaks past the sweep untouched.
# Deliberately narrow: the sentinel must open the line and be followed by a
# punctuation separator, so prose that merely *mentions* the token
# mid-sentence never matches.
_FLUSH_OK_LEAD_RE = re.compile(r"^\s*flush_ok\s*[—–\-:;,.]\s", re.IGNORECASE)
_DELTA_ANCHOR = "Δ bytes:"


def _is_leading_sentinel(line: str) -> bool:
    """True when `line` opens a body with the `FLUSH_OK` sentinel — either alone
    on the line, or trailing a same-line reversal preamble."""
    return bool(_FLUSH_OK_RE.match(line) or _FLUSH_OK_LEAD_RE.match(line))


def _normalize_flush_body(raw: str, kind: str = "flush") -> str | None:
    """Strip sentinel / preamble / narration from a writer's raw output.

    Returns the cleaned body, or None when nothing substantive remains (the
    caller skips the write and logs a WARN).

    kind="flush":
        If the body does NOT open with a `FLUSH_OK` line, return it UNCHANGED.
        Clean output is never stripped — the strip-to-first-heading path is a
        pollution transform and must not run on the healthy 99% path, or the
        day the model emits one lead sentence before `## DECISIONS` a clean
        write silently loses it. Only a body that starts with the sentinel (a
        reversal blob) gets the treatment: drop the sentinel, then drop the
        preamble up to the first heading; None if no heading/substance follows.

    kind="reflection":
        Drop a leading `FLUSH_OK` line if present. If narration precedes the
        first `Δ bytes:` anchor, drop everything before it. With no anchor
        present, return the body as-is (safe no-op — 29 historical sections
        have no anchor and must not be mangled). None if empty.
    """
    if kind not in ("flush", "reflection"):
        raise ValueError(f"unknown kind {kind!r} — expected 'flush' or 'reflection'")

    lines = raw.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return None

    leading_sentinel = _is_leading_sentinel(lines[first])

    if kind == "flush":
        if not leading_sentinel:
            return raw
        rest = lines[first + 1 :]
        head = next((i for i, ln in enumerate(rest) if _HEADING_RE.match(ln)), None)
        if head is None:
            return None
        return "\n".join(rest[head:]).strip() or None

    body = "\n".join(lines[first + 1 :]) if leading_sentinel else raw
    body = body.strip()
    if not body:
        return None
    pos = body.find(_DELTA_ANCHOR)
    return body if pos == -1 else body[pos:]


def _strip_flush_ok_lines(raw: str) -> str | None:
    """Remove whole-line `FLUSH_OK` sentinels, keeping every other byte.

    For the cleanup sweep's Shape D — substantive content with a stray or
    trailing sentinel line, where the leading-sentinel transform above does not
    apply. Inline mentions of the token inside prose are untouched by
    construction (only full-line matches are dropped). None if nothing remains.
    """
    kept = [ln for ln in raw.splitlines() if not _FLUSH_OK_RE.match(ln)]
    return "\n".join(kept).strip() or None


# Where a daily-log record starts: an ATX heading (`## Session … summary`) or a
# bold run-in (`**DECISIONS**`) — the same two shapes the flush summarizer
# emits. Mirrors _HEADING_RE minus the bulleted arm: a `- **FACTS**` bullet is
# mid-record content, not a record boundary.
# The `\w` on the bold arm is deliberate: without it a `***` horizontal rule
# (or a bare `**`) reads as a record boundary and the tail opens on the rule.
# Single source of truth for session-start-context.py and memory_flush.py —
# do not copy this regex into other modules; import it.
RECORD_START_RE = re.compile(r"^(#{1,6}\s|\*\*\w)")


def tail_from_heading(text: str, max_chars: int) -> str:
    """Return the last <= max_chars of `text`, advanced to a record boundary.

    Takes the char-budgeted tail, then drops leading lines until one looks like
    a record start, so the slice never opens mid-entry. If no heading falls
    inside the budget the budgeted tail is returned as-is — a boundary-less
    slice still beats dropping the log entirely.
    """
    if len(text) <= max_chars:
        return text
    lines = text[-max_chars:].splitlines()
    start = next((i for i, ln in enumerate(lines) if RECORD_START_RE.match(ln)), None)
    if start is None:
        return "\n".join(lines).strip()
    return "\n".join(lines[start:]).strip()


# ---------- Cost ledger ----------
#
# Persistent per-day cumulative spend tracker. Flush and reflection both
# write to the same daily bucket so they share a single budget. Used by
# `is_over_budget()` to short-circuit Opus runs once the day's cap is hit.
#
# Storage shape (`.claude/data/state/cost-ledger.json`):
#   {"YYYY-MM-DD": {"flush": 0.42, "reflection": 0.05, "index": 0.001}}
#
# Default cap: $50/day. Override via `BRAIN_DAILY_BUDGET_USD` env var.
#
# The cap is a RUNAWAY FUSE, not a spend target: it sits well above normal
# daily spend so it only trips on genuine pathology — a retry storm or a stuck
# loop — never on a busy day. Sub-caps below are what actually shape spend.
#
# Per-category sub-caps. One shared pool has a starvation problem: the
# spenders do NOT compete fairly. memory_flush fires many times a day, while
# reflection runs once the NEXT morning — so a chatty day can let flush eat
# the whole cap and the curator (the thing that actually improves memory) gets
# skipped. A sub-cap bounds each spender inside the global ceiling.
# Override any of them with `BRAIN_<CATEGORY>_BUDGET_USD`.

import json as _json  # noqa: E402  (avoid shadowing if a caller imports json)

DEFAULT_DAILY_BUDGET_USD = 50.0
# Only categories that can run away need an entry; anything absent is bounded
# by the global cap alone.
DEFAULT_CATEGORY_BUDGETS_USD = {
    "flush": 15.0,  # roughly 100 flushes/day at typical per-call cost — comfortably above a busy day
}


def _cost_ledger_path() -> Path:
    return get_project_root() / ".claude" / "data" / "state" / "cost-ledger.json"


def _today_key() -> str:
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


def _load_ledger() -> dict[str, dict[str, float]]:
    p = _cost_ledger_path()
    if not p.exists():
        return {}
    try:
        raw = _json.loads(p.read_text(encoding="utf-8"))
    except _json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def record_cost(category: str, usd: float) -> None:
    """Append `usd` to today's bucket under `category`. No-op on falsy/None.

    Categories are free-form strings — current callers use 'flush',
    'reflection', 'index', 'compression'. Failures are silent (logging
    a cost should never break the host run).
    """
    if not usd or usd <= 0:
        return
    p = _cost_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    today = _today_key()
    try:
        with file_lock(p, timeout=5.0):
            ledger = _load_ledger()
            day = ledger.setdefault(today, {})
            day[category] = round(float(day.get(category, 0.0)) + float(usd), 6)
            atomic_write(p, _json.dumps(ledger, indent=2, sort_keys=True))
    except Exception:
        pass  # cost logging must never crash the caller


def get_today_cost(category: str | None = None) -> float:
    """Return today's cumulative spend. Pass `category` to filter; default
    sums all categories for the day.
    """
    ledger = _load_ledger()
    day = ledger.get(_today_key(), {})
    if category is not None:
        return float(day.get(category, 0.0))
    return float(sum(day.values()))


def get_daily_budget() -> float:
    raw = os.environ.get("BRAIN_DAILY_BUDGET_USD")
    if not raw:
        return DEFAULT_DAILY_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DAILY_BUDGET_USD


def get_category_budget(category: str) -> float | None:
    """Sub-cap for `category`, or None when it is bounded only by the global cap."""
    raw = os.environ.get(f"BRAIN_{category.upper()}_BUDGET_USD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_CATEGORY_BUDGETS_USD.get(category)


def is_over_budget(category: str | None = None) -> bool:
    """True if today's spend has hit the global cap, or `category`'s sub-cap.

    Callers pass their own category so a single runaway spender fuses itself
    out while leaving headroom for everyone else. Omitting it preserves the
    old global-only behaviour.
    """
    if get_today_cost() >= get_daily_budget():
        return True
    if category is None:
        return False
    cap = get_category_budget(category)
    return cap is not None and get_today_cost(category) >= cap


# ---------- Hook execution log ----------
#
# Cross-hook observability — every early-exit path in our hooks calls
# `log_hook_execution()` so a single flat file answers "what did each hook
# do today?" Backported because silent early-exits
# made post-incident audits painfully slow (had to reverse-engineer outcomes
# from transcript files and log absence).

HOOK_LOG_MAX_LINES = 1000
HOOK_LOG_KEEP_LINES = 500


def _hook_log_path() -> Path:
    return get_project_root() / ".claude" / "data" / "logs" / "hook-execution.log"


def log_hook_execution(
    hook_name: str,
    trigger: str,
    status: str,
    duration_s: float,
    detail: str = "",
) -> None:
    """Append a line to the hook execution log with simple rotation.

    status: 'OK' | 'SKIP' | 'ERROR' | 'BLOCKED'
    Never raises — hook logging must not crash the hook itself.
    """
    timestamp = datetime.now(EASTERN).isoformat()
    line = f"{timestamp} | {hook_name} | {trigger} | {status} | {duration_s:.1f}s"
    if detail:
        line += f" | {detail}"
    log_path = _hook_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            if len(lines) >= HOOK_LOG_MAX_LINES:
                log_path.write_text(
                    "\n".join(lines[-HOOK_LOG_KEEP_LINES:]) + "\n",
                    encoding="utf-8",
                )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def xml_wrap(tag: str, content: str, attrs: dict[str, str] | None = None) -> str:
    attr_str = ""
    if attrs:
        parts = []
        for k, v in attrs.items():
            safe = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            parts.append(f'{k}="{safe}"')
        attr_str = " " + " ".join(parts)
    return f"<{tag}{attr_str}>\n{content}\n</{tag}>"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        target = tmp_path / "state.json"
        with file_lock(target):
            pass
        assert (tmp_path / "state.json.lock").exists(), "lock sibling not created"

        payload = '{"hello": "world"}'
        atomic_write(target, payload)
        assert target.read_text(encoding="utf-8") == payload, "atomic_write round-trip failed"
        assert not Path(f"{target}.tmp").exists(), "tmp file leftover"

        wrapped = xml_wrap("daily_log", "hi", attrs={"date": "2026-04-11"})
        assert wrapped.startswith('<daily_log date="2026-04-11">'), f"xml_wrap header wrong: {wrapped!r}"
        assert wrapped.endswith("</daily_log>"), f"xml_wrap footer wrong: {wrapped!r}"
        assert "\nhi\n" in wrapped, f"xml_wrap body wrong: {wrapped!r}"

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                exc = RuntimeError("boom")
                exc.status = 503  # type: ignore[attr-defined]
                raise exc
            return "ok"

        assert with_retry(flaky, base_delay=0.01) == "ok", "with_retry should eventually succeed"
        assert calls["n"] == 3, f"expected 3 attempts got {calls['n']}"

        # Timeout=None default path (backward-compat): should acquire fresh lock.
        lock_target = tmp_path / "timeout_target"
        with file_lock(lock_target, timeout=1.0):
            pass

        # Timeout against a held lock: manually flock a second handle on the
        # same sibling .lock file, then assert file_lock(timeout=0.1) raises.
        lock_sibling = Path(f"{lock_target}.lock")
        holder = open(lock_sibling, "w")
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            raised = False
            try:
                with file_lock(lock_target, timeout=0.1):
                    pass
            except TimeoutError:
                raised = True
            assert raised, "file_lock(timeout=0.1) should raise against a held lock"
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

        # log_hook_execution: write a line, verify rotation trims to KEEP when MAX exceeded.
        fake_project = tmp_path / "fakeproject"
        fake_project.mkdir()
        (fake_project / "CLAUDE.md").write_text("test", encoding="utf-8")
        old_env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(fake_project)
        try:
            log_hook_execution("selftest", "trigger", "OK", 0.1, "hello")
            log_path = fake_project / ".claude" / "data" / "logs" / "hook-execution.log"
            assert log_path.exists(), "hook-execution.log not created"
            assert "selftest | trigger | OK | 0.1s | hello" in log_path.read_text(encoding="utf-8")

            log_path.write_text("\n".join(f"line {i}" for i in range(HOOK_LOG_MAX_LINES + 1)) + "\n", encoding="utf-8")
            log_hook_execution("selftest", "trigger", "OK", 0.0, "rotation")
            after = log_path.read_text(encoding="utf-8").splitlines()
            assert len(after) == HOOK_LOG_KEEP_LINES + 1, f"rotation kept {len(after)} lines"
            assert after[-1].endswith("rotation"), "newest line not at tail"
        finally:
            if old_env is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old_env

        print("SHARED_SELFTEST_OK")
