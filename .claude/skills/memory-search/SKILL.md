---
name: memory-search
description: |
  Semantic/fuzzy/paraphrase search over the markdown vault at Vault/Memory/
  — hybrid RAG (FastEmbed + FTS5 + Reciprocal Rank Fusion). Reach for it when
  there's no clear path or literal anchor to go by: recalling a
  half-remembered decision, tracking down a fact by fuzzy description, "what did
  I say about X", "do I have anything on W", "remember when I decided A".
  Do NOT use when the file path is already known (today's daily log,
  SOUL.md, USER.md, MEMORY.md, a specific project file) — Read directly.
---

# Memory Search

Wraps `.claude/scripts/memory_search.py` with the heuristics you need to use it well. The script is a hybrid RAG pipeline: FastEmbed `bge-small-en-v1.5` (384d) + sqlite-vec KNN + FTS5 BM25 merged with Reciprocal Rank Fusion (k=60). It's already tuned for the vault — your job is to know **when to reach for it**, **how to phrase queries**, **how to read the output**, and **what to do when it misses**.

## Step 1 — Decide whether to search at all

Most lookups in a second-brain don't need RAG. Route to the peer mechanisms first:

1. **Known path** → `Read` directly (today's daily log, USER.md, SOUL.md, MEMORY.md, a specific project file the user named).
2. **Literal anchor** (a name, an ID, a URL, an exact phrase) → `Grep` first. Embeddings are weak on literals; FTS5 helps but grep is faster and more precise.
3. **What's left is this skill's regime**: paraphrase/fuzzy queries with no clear path or literal anchor. Note `MEMORY.md` + **today's** daily log are injected at SessionStart — scroll back before searching. Proceed to Step 2.

## Step 2 — Phrase the query for bge-small-en-v1.5

- **No prefix.** BGE v1.5 uses **no** instruction prefix. Do NOT prepend `"query:"` (that's the E5 convention). The pipeline already respects this.
- **Phrase as a noun phrase topic, not a question.** Embeddings rank by topical similarity.
  - Good: `"Q4 pipeline planning notes"`
  - Bad: `"what did I write about Q4 pipeline planning?"`
- **Include 1-2 domain terms from the vault's own vocabulary** where possible. If the vault says "quarterly review" not "QBR", match its words.
- **Avoid over-short queries (<3 tokens)** and pronoun-heavy queries. They dilute the embedding.
- **One concept per query.** Split compound questions into multiple searches.

## Step 3 — Run the search

```bash
uv run python .claude/scripts/memory_search.py "<query>" [--k N] [--path-prefix PREFIX] [--min-score FLOAT]
```

Flags:
- `--k N` (default `10`) — top-k results returned after RRF merge
- `--path-prefix PREFIX` — restrict to `Vault/Memory/<PREFIX>/` (e.g. `daily`, `projects`)
- `--min-score FLOAT` — drop results below this RRF score floor

**Default first shot:** bare query, no flags. Don't preemptively narrow with `--path-prefix` — you don't know where it lives yet, that's why you're searching.

Output format (plain text, one result per line):
```
[score 0.0334] Vault/Memory/people/alex-rivera.md:Acme Corp
    Alex is the Head of Ops at Acme Corp, reports into the COO. Works closely w...
```

## Step 4 — Interpret the scores

RRF scores are **ordinal, not probabilistic**. With k=60:
- Single-list top hit (one retriever ranked it #1) caps at ~0.0164
- **Score > ~0.03 means BOTH retrievers ranked it highly** — that's the strong signal
- Score > ~0.02 generally signals a real semantic match
- Score < ~0.02 is usually FTS5 stopword noise — ignore

Treat the RRF score as "how much the two retrievers agreed", not as a probability. A 0.04 is meaningfully better than a 0.025; a 0.025 is meaningfully better than a 0.018; the absolute values mean nothing beyond that.

## Step 5 — Read the full file of top hits

**Chunks are previews, not ground truth.** After you've picked the top 1-3 hits:
- `Read` the full file. Frontmatter, checklist state, surrounding headings, and the rest of the document often carry meaning the chunk snippet strips.
- If multiple top-k hits point to the same file, Read it once and skip the rest.
- Only trust the chunk text directly if the question is answerable from 120 characters (rare).

## Step 6 — Retry ladder on misses

If the first search returns `(no results)` or nothing above 0.02, walk this ladder. **Stop at 3 attempts and report "not found" rather than loop.**

1. **Widen k, drop floor.** `--k 25 --min-score 0.01`. Catches weak but real matches.
2. **Reformulate.** Swap synonyms, add domain nouns from the vault's vocabulary, split compound queries into two searches. Try the noun-phrase rule again.
3. **Path-prefix fallback.** If you have a strong prior on which folder holds it, rerun with `--path-prefix projects` / `daily`.

After 3 misses, tell the user "I couldn't find anything in the vault matching X" — don't burn cycles on a 4th retry.

## Step 7 — Stale-index check

A cron job runs `memory_index.py` every 15 minutes (at :07 / :22 / :37 / :52), so the index is never more than ~15 min behind disk. You only need to reindex manually when searching for something written **in this session** or **within the last ~15 min** — e.g. a file the user just saved, today's daily log mid-edit, a note written earlier in this conversation.

If you suspect staleness, run:

```bash
uv run python .claude/scripts/memory_index.py
```

Incremental reindex (adds new files, updates changed ones, drops orphans). Takes a few seconds. Then re-run the search. If the incremental reindex still doesn't surface it, `--full` rebuilds from scratch (slower, rarely needed).

## Anti-patterns

- **Don't search for today's daily log.** Read `Vault/Memory/daily/<today>.md` directly.
- **Don't search for MEMORY.md / SOUL.md / USER.md content.** They're already in your context from SessionStart.
- **Don't search for exact string literals (IDs, URLs, names in quoted form).** Grep is faster and more precise.
- **Don't chain more than 3 retries.** If the vault doesn't have it, reporting "not found" is the right answer.
- **Don't trust a chunk snippet as the full answer.** Always Read the full file before acting on it (writing a draft, drafting a response, citing a decision).
- **Don't add instruction prefixes to the query.** BGE v1.5 is symmetric — no `"query:"`, no `"Represent this sentence for searching:"`.
