---
name: import-recent-work
description: Bootstrap or refresh the second brain's memory from the user's actual recent work — scan their Claude Code session history (~/.claude/projects/) AND all git repos under their code directories (catching work done in other tools), draft one-page project state files via parallel subagents, and seed MEMORY.md Active Projects after the user confirms what matters. Use when the user says "import my recent work", "pull in my projects", "bring in my Claude Code history", "bootstrap my memory", "you should know what I've been working on", "catch up on my repos", or during first-time setup when the user wants existing work loaded into memory. Not for importing markdown notes (drop those in Vault/Memory/imported/ and reindex).
---

# Import Recent Work

Turn the user's existing Claude Code history and git activity into memory: one
`projects/<slug>.md` state page per active project, plus seeded
`## Active Projects` bullets in MEMORY.md.

## Context rules (non-negotiable)

- **Subagents return pointers, never payloads.** All drafts go to disk at
  `.claude/data/onboarding/drafts/`. Each subagent's reply is capped at ~10
  lines. Never let transcript or draft content flow into the main conversation.
- **Only the main session writes MEMORY.md** — exact headings, single writer.
- **Consent before scanning.** Transcripts can contain pasted credentials and
  private data. Ask before reading any transcript content. (The discovery
  script is safe — it reads only path metadata.)

## Workflow

### 1. Discover (zero tokens)

```bash
python3 .claude/skills/import-recent-work/scripts/discover.py 30
```

Two passes, merged, emitted as JSON: (1) every project with Claude Code
sessions in the window (default 30 days) — real path, session count, newest
transcript paths; (2) a git scan of the user's code roots (inferred from the
parents of the Claude Code project paths) that catches repos worked on in
OTHER tools — `"source": "git-only"`. If the user keeps code somewhere
unusual, add `--roots ~/somewhere ~/else`.

The script filters noise itself (tmp scratch, worktree checkouts, dead
directories, top 15 by recency) and lists what it dropped under
`filtered_out`. If the user expected something that isn't there, check
`filtered_out` first, then re-run with `--all`. Entries carrying
`"worktree_of": <parent>` are linked git worktrees — discuss them as part of
their parent repo, not as separate projects.

### 2. Confirm scope

Show the filtered list and ask two things in one message:

1. "Which of these are active — things you'll touch in the next couple of
   weeks?" (aim for 3–7; more dilutes memory)
2. "OK to read the recent session transcripts for those, to write up where
   each project stands?"

Also ask about anything they expected to see but didn't — rerun with
`--roots` for extra directories, or add named repos manually.

Seed **only genuinely active** projects: the nightly reflection archives any
MEMORY.md bullet not referenced in daily logs within ~14 days, so padding the
list just creates churn.

### 3. Fan out draft subagents (one per confirmed project, in parallel)

```bash
mkdir -p .claude/data/onboarding/drafts
```

Launch one Task subagent per confirmed project with this brief:

> Write a one-page project state file to
> `.claude/data/onboarding/drafts/<slug>.md` for the project at `<path>`.
> Sources, in order: (1) the conversational content of the 2–3 newest
> transcripts listed for it — for each, run
> `python3 .claude/skills/import-recent-work/scripts/extract_transcript.py <transcript path>`
> and read its output (user turns + assistant text only; never read the raw
> .jsonl, which is full of tool payloads). Skip this source for git-only
> projects, which have no transcripts. (2)
> `git -C <path> log --since='30 days ago' --stat | head -100`; (3) the
> repo README if one exists. Structure the file: YAML frontmatter (`title`,
> one-line `description`), then `## What it is`, `## Where it lives` (path,
> remote, branch), `## Current state`, `## Next steps / open questions`.
> Hard cap one page. Never copy credentials, tokens, or API keys into the
> draft even if they appear in transcripts. Reply with EXACTLY: the slug, a
> 1–2 line current-state summary, up to 3 flagged candidates for long-term
> memory (decisions made, key facts, recurring people/tools), and the draft
> path. No other content.

Pick slugs: short kebab-case from the directory name.

### 4. One confirmation menu

Assemble the subagent replies into a single menu — per project: the 1–2 line
state summary plus its flagged candidates. Ask the user to approve, edit, or
drop each project and each flagged fact/decision. One message, not an
interrogation per project.

### 5. Promote and seed (main session only)

For each approved project:

```bash
mv .claude/data/onboarding/drafts/<slug>.md Vault/Memory/projects/<slug>.md
```

Apply any user edits with targeted Edits — do not read whole drafts into
context unless the user asks what's in one. Delete rejected drafts (they are
staging files, not vault files — plain `rm` from the staging dir is fine).

Then update `Vault/Memory/MEMORY.md`:

- Under the existing `## Active Projects` heading (exact text, never a
  variant): one bullet per project, **1–2 lines max**, built from the
  subagent's state summary — current state + next step +
  `See projects/<slug>.md`.
- Approved facts/decisions: user-approved cross-project facts go under
  `## Important Facts`; leave `## Key Decisions` and `## Strategic Threads`
  alone unless the user explicitly asks — those must earn their place through
  daily logs, and the reflection prunes hand-seeded entries aggressively.
- Keep MEMORY.md under 15 KB — it is injected into every session.

### 6. Close the loop

Append a short entry to today's daily log (`Vault/Memory/daily/YYYY-MM-DD.md`,
Eastern date, append-only) recording the import: which projects were seeded
and any approved facts. This gives the first nightly reflection a citation for
every new bullet, so nothing gets flagged stale on night one.

Then run the indexer so the new project pages are searchable:

```bash
uv run python .claude/scripts/memory_index.py
```

Before the recap, check whether the nightly reflection is actually scheduled:

```bash
crontab -l 2>/dev/null | grep -F "$(pwd)"
```

Finish with a 2–3 sentence recap: what's now in active memory and what's
searchable. If the cron check found a reflection entry, say the nightly
reflection maintains memory from here. If it found nothing, say memory will
NOT self-maintain until reflection is scheduled, and point the user at
`cron.example` in the repo root.

## What NOT to do

- Do not backfill daily logs from old transcripts — project pages carry the
  state; historical logs would just feed the reflection stale material.
- Do not scan directories beyond what discover.py found plus what the user
  names.
- Do not write imported notes or transcript content into MEMORY.md directly.
- Do not exceed ~7 seeded projects even if the user lists more — suggest
  project pages without MEMORY.md bullets for the overflow.

## Re-runs

Safe to re-run anytime ("catch up on my repos"): discover again, diff against
existing `Vault/Memory/projects/` pages and `## Active Projects` bullets,
and only draft/update what changed or is new. Update existing bullets in
place; never duplicate.
