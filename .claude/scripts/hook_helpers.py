from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from shared import get_project_root, log_hook_execution  # noqa: E402


MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 5


def _read_stdin_json() -> dict:
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


def extract_text_from_content(content: object) -> str:
    """Extract readable text from a message content field.

    Content can be a string or a list of content blocks. Only blocks with
    type == "text" contribute. Tool-use, tool-result, thinking, and any
    other block types are filtered out.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last N conversation turns as markdown.

    Returns:
        (context_string, turn_count). turn_count is the number of turns
        actually included after the last-MAX_TURNS slice.

    Filters:
        - Only user/assistant messages (system, attachment, etc. dropped)
        - Only text-typed content blocks (tool_use/tool_result/thinking dropped)
        - Empty text stripped

    Truncation: if the joined markdown exceeds MAX_CONTEXT_CHARS, take the
    tail and snap to the next `**User:**`/`**Assistant:**` turn boundary.
    """
    turns: list[dict[str, str]] = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") if isinstance(entry, dict) else None
                if isinstance(msg, dict):
                    role = msg.get("role")
                    content = msg.get("content", "")
                else:
                    role = entry.get("role") if isinstance(entry, dict) else None
                    content = entry.get("content", "") if isinstance(entry, dict) else ""
                if role not in ("user", "assistant"):
                    continue
                text = extract_text_from_content(content).strip()
                if not text:
                    continue
                label = "User" if role == "user" else "Assistant"
                turns.append({"role": label, "text": text})
    except FileNotFoundError:
        return "", 0

    recent = turns[-MAX_TURNS:]
    parts = [f"**{t['role']}:** {t['text']}\n" for t in recent]
    context = "\n".join(parts)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary >= 0:
            context = context[boundary + 1:]

    return context, len(recent)


def spawn_flush() -> int:
    start = time.time()
    if os.environ.get("CLAUDE_INVOKED_BY"):
        log_hook_execution("spawn_flush", "unknown", "SKIP", time.time() - start, "CLAUDE_INVOKED_BY set")
        return 0

    payload = _read_stdin_json()
    session_id = str(payload.get("session_id") or "unknown").strip() or "unknown"
    transcript_path_s = payload.get("transcript_path")
    if not transcript_path_s:
        log_hook_execution("spawn_flush", session_id, "SKIP", time.time() - start, "no transcript")
        return 0

    project_root = get_project_root()
    flush_dir = project_root / ".claude" / "data" / "flush"
    log_dir = project_root / ".claude" / "data" / "logs"
    flush_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    src = Path(transcript_path_s)
    try:
        context, turn_count = extract_conversation_context(src)
    except Exception as e:
        log_hook_execution("spawn_flush", session_id, "ERROR", time.time() - start, f"extraction: {e}")
        return 0

    if turn_count < MIN_TURNS_TO_FLUSH or not context.strip():
        log_hook_execution("spawn_flush", session_id, "SKIP", time.time() - start, f"{turn_count} turns")
        return 0

    context_path = flush_dir / f"{session_id}.md"
    try:
        context_path.write_text(context, encoding="utf-8")
    except OSError as e:
        log_hook_execution("spawn_flush", session_id, "ERROR", time.time() - start, f"write: {e}")
        return 0

    venv_python = project_root / ".venv" / "bin" / "python"
    flush_script = project_root / ".claude" / "scripts" / "memory_flush.py"
    stderr_log = open(log_dir / "memory_flush.stderr.log", "a", encoding="utf-8")

    env = {**os.environ, "CLAUDE_INVOKED_BY": "memory_flush"}

    try:
        subprocess.Popen(
            [
                str(venv_python),
                str(flush_script),
                "--transcript",
                str(context_path),
                "--session",
                session_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            start_new_session=True,
            env=env,
            cwd=str(project_root),
        )
    except OSError as e:
        log_hook_execution("spawn_flush", session_id, "ERROR", time.time() - start, f"spawn: {e}")
        return 0

    log_hook_execution("spawn_flush", session_id, "OK", time.time() - start, f"{turn_count} turns")
    return 0


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # --- Fixture 1: mixed 10-entry JSONL, 6 valid turns ---
        fixture = tmp_path / "mixed.jsonl"
        entries = [
            # 1. Real user message (string content, nested under message)
            {"type": "user", "message": {"role": "user", "content": "hey what's up"}},
            # 2. Assistant reply (list of blocks, text only)
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "not much, you?"}
            ]}},
            # 3. Legacy top-level shape (no nested message)
            {"role": "user", "content": "top-level shape still works"},
            # 4. Assistant thinking block ONLY — filtered out (no text block)
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"}
            ]}},
            # 5. User tool_result — filtered out (no text block)
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "ok"}
            ]}},
            # 6. Assistant with tool_use + text — text kept
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {}},
                {"type": "text", "text": "here's the file"}
            ]}},
            # 7. System message — filtered out (wrong role)
            {"type": "system", "message": {"role": "system", "content": "system blurb"}},
            # 8. Empty string content — filtered out
            {"type": "user", "message": {"role": "user", "content": ""}},
            # 9. Real user follow-up
            {"type": "user", "message": {"role": "user", "content": "ok thanks"}},
            # 10. Attachment entry (no message key, no role) — filtered out
            {"type": "attachment", "attachment": {"content": "ignored"}},
            # 11. Bonus real assistant
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "you got it"}
            ]}},
        ]
        with open(fixture, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        context, n = extract_conversation_context(fixture)
        assert n == 6, f"expected 6 turns, got {n}"
        assert "**User:**" in context, f"missing User marker: {context!r}"
        assert "**Assistant:**" in context, f"missing Assistant marker: {context!r}"
        assert "hey what's up" in context
        assert "not much, you?" in context
        assert "top-level shape still works" in context
        assert "here's the file" in context
        assert "ok thanks" in context
        assert "you got it" in context
        # No JSON noise leaked through
        assert '{"type":' not in context, f"JSON noise leaked: {context[:200]!r}"
        assert "tool_use" not in context, f"tool_use leaked: {context[:200]!r}"
        assert "thinking" not in context, f"thinking leaked: {context[:200]!r}"
        assert "system blurb" not in context, "system role leaked"

        # --- Fixture 2: truncation path ---
        trunc_fixture = tmp_path / "big.jsonl"
        big_text = "x" * 1000
        with open(trunc_fixture, "w", encoding="utf-8") as f:
            for i in range(40):
                e = {"type": "user", "message": {"role": "user", "content": big_text}}
                f.write(json.dumps(e) + "\n")
                e = {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": big_text}
                ]}}
                f.write(json.dumps(e) + "\n")
        big_context, big_n = extract_conversation_context(trunc_fixture)
        assert big_n <= MAX_TURNS, f"last-N slice missed: {big_n}"
        assert len(big_context) <= MAX_CONTEXT_CHARS, f"truncation failed: {len(big_context)}"
        assert big_context.startswith("**User:**") or big_context.startswith("**Assistant:**"), \
            f"truncation did not snap to turn boundary: {big_context[:50]!r}"

        # --- Fixture 3: MIN_TURNS gate ---
        tiny_fixture = tmp_path / "tiny.jsonl"
        with open(tiny_fixture, "w", encoding="utf-8") as f:
            for i in range(3):
                e = {"type": "user", "message": {"role": "user", "content": f"msg {i}"}}
                f.write(json.dumps(e) + "\n")
        tiny_context, tiny_n = extract_conversation_context(tiny_fixture)
        assert tiny_n == 3 < MIN_TURNS_TO_FLUSH, f"MIN_TURNS gate broken: {tiny_n}"

        # --- Fixture 4: missing file ---
        missing_context, missing_n = extract_conversation_context(tmp_path / "nope.jsonl")
        assert missing_context == "" and missing_n == 0, "missing file should return ('', 0)"

        print("HOOK_HELPERS_SELFTEST_OK")
