# Second Brain Template

Claude Code that remembers. Every session gets written to a daily log. A morning job turns those logs into long-term memory. Everything is searchable. You use Claude Code like normal and the plumbing runs itself.

I built this because I kept re-explaining the same context to Claude every session. Decisions, project state, who's who. This fixes that.

## What you get

- **Session capture.** When a session ends, a background job writes the decisions, facts, and open questions to an append-only daily log. Q&A-only sessions with nothing worth keeping get skipped on purpose.
- **Morning reflection.** At 8am a job reads yesterday's log and curates it into `Vault/Memory/MEMORY.md` and project files. That file loads into every session.
- **Semantic search.** An indexer runs every 15 minutes. Ask "what did I decide about X" inside Claude and it finds it.
- **Guardrails.** Hooks block Claude from reading `.env` or token files, block `rm`, and stop it from editing its own identity file. Files never get deleted, only moved to `Vault/Memory/trash/`.
- **A spend fuse.** Both AI jobs bill against a daily cap (default **$50/day**) and stop when it's hit.

## The two files that matter

- **`Vault/Memory/SOUL.md`** — who the assistant is. Rules, tone, hard limits. Loaded every session.
- **`Vault/Memory/USER.md`** — who you are. Also loaded every session.

Keep both short. Real facts and real rules help. Persona flavor text and biography don't. Everything else in `Vault/Memory/` is the assistant's memory and it manages that itself.

## Setup

Takes about 10 minutes.

1. Install [Claude Code](https://docs.claude.com/en/docs/claude-code) and [uv](https://docs.astral.sh/uv/). You need Python 3.12 on macOS or Linux. Native Windows doesn't work (no cron, and the file locking is POSIX-only).
2. In this repo:
   ```bash
   uv sync
   mkdir -p .claude/data/logs
   ```
3. Run `claude` in this repo and say **"set me up"**. The `setup-assistant` skill asks a few questions, fills in `SOUL.md` and `USER.md`, builds the search index (first run downloads a ~130MB embedding model, slow once, fast after), and hands you the exact cron lines to paste.

   Want to do it by hand? Fill in the two files, run `uv run python .claude/scripts/memory_index.py --full`, then follow `cron.example`.
4. Done. Use `claude` in this repo from now on.

**Auth:** on a Claude subscription (Pro/Max), log in once via `claude` and the background jobs reuse it. No API key needed. If the morning reflection fails with an auth error, create a `.env` at the repo root with `ANTHROPIC_API_KEY=sk-ant-...`. The assistant can't read that file. A hook blocks it.

## Searching your memory

Inside Claude, just ask. The `memory-search` skill handles it. From the terminal:

```bash
uv run python .claude/scripts/memory_search.py "your query" --k 5
```

## Seeding memory from past work

You don't have to start from zero. The `import-recent-work` skill reads your existing Claude Code history and git repos and bootstraps the vault from them. Say "import my recent work" inside Claude.

## Settings (optional)

Gotcha: set these in **two places**. A `.env` at the repo root (read by the cron jobs) **and** your shell profile like `~/.bashrc` (read by the per-session flush, which inherits your terminal and never reads `.env`). Set only one and you've only capped half the jobs.

| Variable | Default | What it does |
|----------|---------|--------------|
| `BRAIN_DAILY_BUDGET_USD` | `50` | Daily AI-spend cap. Flush and reflection both stop when the day's total hits it. |
| `BRAIN_VAULT_ROOT` | `Vault/Memory` in this repo | Put the vault somewhere else, like inside an existing Obsidian vault. |
| `BRAIN_TIMEZONE` | `America/New_York` | Timezone for daily-log dates and what "yesterday" means to the reflection job. Any IANA name, e.g. `Europe/London`. Setup sets this for you. Cron *times* still run on your machine's clock. |
| `MEMORY_DB` | sqlite | Search index backend. Only sqlite exists. |

Spend is tracked at `.claude/data/state/cost-ledger.json`, one entry per day per job.

## Troubleshooting

- **`memory_index.py` fails with an SQLite extension error.** Your system Python was built without SQLite extension loading. Install a standard Python 3.12 (package manager or python.org) and re-run `uv sync`.
- **Daily logs are empty after sessions.** Check `.claude/data/logs/hook-execution.log` and `.claude/data/logs/memory_flush.log`. Pure Q&A sessions are skipped on purpose.
- **`RuntimeError: aclose()` in cron logs.** Cosmetic SDK teardown noise. The job finished (exit 0).
- **Reflection says `REFLECT_NOOP`.** Normal. No daily log yesterday, nothing to curate.
- **Reflection or flush says `OVER_BUDGET`.** The daily cap tripped. Raise `BRAIN_DAILY_BUDGET_USD` or wait until tomorrow.

## What's in the repo

```
CLAUDE.md                 project instructions (scripts find the repo root by it, don't move it)
Vault/Memory/             the vault: SOUL.md, USER.md, MEMORY.md, daily/, projects/, trash/
.claude/settings.json     hook wiring, native auto-memory off, deny rule for reading .mcp.json
.claude/hooks/            session-start context, end-of-session flush, secret blocking, delete guard
.claude/scripts/          flush, reflection, indexer, search, cron wrappers
.claude/skills/           setup-assistant, memory-search, import-recent-work
cron.example              the two crontab lines to install
```

Rules the assistant lives by: daily logs are append-only. Nothing gets deleted, only moved to `trash/`. `MEMORY.md` stays under 200 lines. `.env` is never read.

## What this is not

Not a chat app. Not a hosted service. It's a repo you run Claude Code inside of, with cron doing the memory work in the background. If you want the vault somewhere else, point `BRAIN_VAULT_ROOT` there.
