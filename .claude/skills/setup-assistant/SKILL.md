---
name: setup-assistant
description: |
  First-run onboarding for this second brain. Interviews the user, writes a
  lean USER.md and tunes SOUL.md from their answers, then health-checks the
  install (venv, search index, cron jobs) and prints fix commands for anything
  missing. Use when the user says "set me up", "setup", "onboard me", "get
  started", "fill in my profile", "configure this", asks how to start using
  the second brain, or when a <setup_hint> section appears in the session
  start context (meaning USER.md or SOUL.md still contain template
  placeholders). Also use to RE-run onboarding later ("update my profile",
  "redo my setup"). Not for memory search or day-to-day vault work.
---

# Setup Assistant

Turn the two placeholder identity files into lean, working ones via a short
interview, then verify the install. Both files are injected into **every**
session, so what goes in them is governed by evidence, not vibes:

- **Persona prose does nothing.** Research (arXiv 2311.10054) shows persona
  text in system prompts does not improve output quality. Do NOT write "you
  are a brilliant expert assistant" flavor text. What works: hard behavioral
  rules and format instructions.
- **Real user context helps a lot** — but only task-relevant facts
  (personalization benchmarks show 12–15% gains on user-defined tasks).
- **Bloat actively hurts.** Irrelevant context measurably degrades reasoning
  (arXiv 2302.00093), and it's paid on every session. **Hard caps: USER.md
  ≤ 60 lines, SOUL.md ≤ 60 lines.** Fewer good lines beat more lines.

## Step 1 — Interview

Ask in **two batches**, not one wall of questions. Number the questions so the
user can answer in one message. Every question maps to a file section — do not
ask anything that doesn't.

**Batch 1 (who you are — feeds USER.md):**
1. Name, and what should the assistant call you?
2. What's your role / what do you actually do day to day?
3. Timezone / city?
4. What platforms should the assistant know about? (email provider, calendar,
   chat, code hosting — names and handles only, **never passwords or API keys**)
5. Rough weekly shape — work hours, standing commitments? (helps the
   assistant reason about timing; skip if you don't care)

**Batch 2 (how it should behave — feeds both files):**
6. How do you like answers: short vs detailed? bullets vs prose? casual vs
   formal? Any pet peeves (e.g. "stop saying 'great question'")?
7. Hard rules — anything the assistant must NEVER do without asking? (e.g.
   systems that are read-only, topics that are off-limits, "never contact
   anyone on my behalf")
8. What will you mostly use this for? (drafting, project tracking, research,
   coding — shapes the one-line mission statement in SOUL.md)

Accept partial answers. Anything skipped: leave that section out of the file
entirely — do not write empty headings or "TBD" filler.

## Step 2 — Write USER.md

Rewrite `Vault/Memory/USER.md` from the answers. Keep the existing YAML
frontmatter (`tags`, `aliases`) unchanged. Remove all `<!-- ... -->`
placeholder comments. Structure:

```markdown
# User Profile

- **Name:** ...
- **Role:** ...
- **Timezone:** ...

## Platforms & IDs
## Professional Background   (2–4 bullets max)
## Communication Style        (their answer to Q6 as concrete rules)
## Schedule                   (only if they answered Q5)
```

Rules:
- ≤ 60 lines total. Bullets, not paragraphs.
- Facts only — no aspirational fluff ("values excellence"), no biography
  beyond what changes how the assistant should act.
- Never write secrets, tokens, or passwords into this file, even if the user
  volunteers them. Say why and leave them out.

## Step 3 — Tune SOUL.md

Edit `Vault/Memory/SOUL.md` surgically — do NOT rewrite the whole file:

1. Replace the placeholder sentence ("The user is a professional who...")
   with 1–2 factual sentences from Q2 + Q8: who the user is and what the
   assistant helps with. Plain description, no persona flourish.
2. Append the user's hard rules from Q7 as `- NEVER ...` bullets under
   **Advisor Mode**, keeping the four stock rules. Remove the `<!-- Add your
   own hard rules -->` comment.
3. Adjust the **Communication Style** bullets to match Q6 — edit or add
   concrete rules, delete any stock rule the user contradicted. Remove the
   `<!-- Tune these -->` comment.
4. Leave **When Uncertain** and the frontmatter alone.

≤ 60 lines total after editing.

## Step 3.5 — Timezone

If the user's timezone (Q3) is anything other than `America/New_York`:

1. Append `BRAIN_TIMEZONE=<their IANA timezone>` to `.env` at the repo root
   (create the file if missing). This covers the cron jobs.
2. Tell them to add `export BRAIN_TIMEZONE=<tz>` to their shell profile
   (`~/.bashrc` or `~/.zshrc`) — this covers the per-session flush, which
   inherits the terminal environment and never reads `.env`. Print the exact
   line to paste.

Use the real IANA name (`Europe/London`, `America/Chicago`), not an
abbreviation. If they gave a city, map it yourself and confirm.

## Step 4 — Health check

Run each check; collect results instead of stopping at the first failure.
(All paths relative to the repo root — the directory containing `CLAUDE.md`.)

| Check | How | Fix to print if missing |
|---|---|---|
| venv | `test -d .venv` | `uv sync` |
| logs dir | `test -d .claude/data/logs` | `mkdir -p .claude/data/logs` |
| search index | `test -f .claude/data/memory.db` | `uv run python .claude/scripts/memory_index.py --full` (first run downloads a ~130MB model — slow once) |
| cron jobs | `crontab -l 2>/dev/null \| grep -c "$(pwd)"` ≥ 2 | open `cron.example`, follow its 3-step instructions |

If the index is missing and the user agrees, run the build for them (it's
safe and idempotent). Do not edit their crontab yourself — cron is the one
step they do by hand; just show the two lines from `cron.example` with
`<REPO>` already substituted with the real absolute path.

## Step 5 — Confirm

Show the user the final USER.md and the diff-worthy parts of SOUL.md
(mission sentence, added rules), report the health-check table with ✅/❌ per
row plus fix commands. Then offer one optional next step: "Want me to import
your recent work? (the import-recent-work skill scans your Claude Code
history and git repos to seed project memory)". Close with: "You're set —
just use `claude` in this repo normally from now on. Say 'update my profile'
anytime to redo this."
