# Second Brain — Project Instructions

## What this is
A personal AI second brain built on Claude Code + Claude Agent SDK. Memory lives in a markdown vault at `Vault/Memory/`; hooks capture every session, a background flush summarizes them into daily logs, a nightly reflection curates those logs into long-term memory, and a local RAG index makes it all searchable.

> This file is load-bearing: scripts resolve the project root by walking up until they find `CLAUDE.md`. Do not delete or move it.

## Key paths
- `Vault/Memory/` — the markdown vault (source of truth for memory)
- `Vault/Memory/SOUL.md` — the assistant's identity and rules (edit this to shape behavior)
- `Vault/Memory/USER.md` — who you are (fill this in)
- `Vault/Memory/MEMORY.md` — curated active memory, loaded into every session
- `Vault/Memory/daily/` — append-only daily logs (YYYY-MM-DD.md)
- `Vault/Memory/projects/` — project context files
- `Vault/Memory/trash/` — soft-deleted files (never hard-delete)
- `.claude/hooks/` — Claude Code hook scripts (session capture, guardrails)
- `.claude/scripts/` — Python scripts (flush, reflection, indexing, search)
- `.claude/data/state/` — JSON state files (cost ledger, dedup state)
- `.env` — optional secrets (only `ANTHROPIC_API_KEY` if needed for cron)

## Conventions
- **Timezone:** America/New_York (Eastern) is baked into the scripts (`shared.EASTERN`). All daily-log dating uses it.
- **Daily logs are append-only** — never rewrite history.
- **MEMORY.md stays under 200 lines.** The nightly reflection promotes/demotes content; don't let it bloat.
- **Soft-delete only:** move vault files to `Vault/Memory/trash/` instead of deleting. A hook enforces this.
- **Secrets rule:** never read `.env` or print env vars. A PreToolUse hook enforces this.
- **Single memory system — the vault.** Claude Code's native auto-memory is disabled (`autoMemoryEnabled: false` in `.claude/settings.json`). `Vault/Memory/` is the sole memory store.
- **SOUL.md is write-protected during reflection runs** via a hook the reflection script loads programmatically; regular interactive sessions can edit it freely.

## Dev environment
- **Package manager:** `uv` is authoritative. `pyproject.toml` + `uv.lock` are source of truth. Never use `pip install` or `requirements.txt`.
- **Python:** 3.12, pinned via `.python-version`.
- **Running scripts interactively:** `uv run python .claude/scripts/<name>.py ...`
- **Running scripts from hooks / cron:** use `.venv/bin/python` directly (hooks and cron wrappers already do this).
- **If `.venv/` is missing or broken:** run `uv sync`.
