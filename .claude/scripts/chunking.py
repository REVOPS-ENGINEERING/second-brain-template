"""Heading-aware markdown chunker.

Splits markdown into ~400-token chunks, respecting heading boundaries. Within
oversized sections, falls back recursively to paragraph -> single newline ->
sentence, with 50-token overlap between adjacent intra-section chunks. Overlap
never crosses a heading boundary.

tiktoken's o200k_base is a proxy counter — BGE uses BERT WordPiece, but the
ratio is close enough within the 400-of-512 safety margin.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import tiktoken

_DEFAULT_TARGET = 400
_DEFAULT_OVERLAP = 50
# min_tokens=20 filters out boilerplate stubs like "(populated by the
# nightly reset)" (7 tokens) and "(to be filled)" (4 tokens).
# Small files whose every section falls below this threshold (e.g. people
# profile stubs) are still captured via the whole-file fallback in
# chunk_markdown.
_DEFAULT_MIN = 20

_enc: Optional[tiktoken.Encoding] = None


def _encoding() -> tiktoken.Encoding:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("o200k_base")
    return _enc


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def split_sections(text: str, filename: str) -> list[dict]:
    """Split on ATX headings (#..######), tracking a running heading stack.

    Returns a list of {"heading_path": str, "body": str}. `heading_path` is
    "filename > h1 > h2 > h3" style. The pre-heading preamble (if any) is
    emitted under just the filename.
    """
    # Find all heading positions
    matches = list(_HEADING_RE.finditer(text))
    sections: list[dict] = []

    base = filename

    if not matches:
        body = text.strip()
        if body:
            sections.append({"heading_path": base, "body": body})
        return sections

    # Preamble before the first heading
    first_start = matches[0].start()
    preamble = text[:first_start].strip()
    if preamble:
        sections.append({"heading_path": base, "body": preamble})

    # Heading stack: list of (level, title)
    stack: list[tuple[int, str]] = []

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()

        # Pop stack until top has smaller level
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        if not body:
            continue

        path_parts = [base] + [t for _, t in stack]
        heading_path = " > ".join(path_parts)
        sections.append({"heading_path": heading_path, "body": body})

    return sections


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def _split_by_delimiter(text: str, delim: str) -> list[str]:
    parts = [p.strip() for p in text.split(delim)]
    return [p for p in parts if p]


def split_oversized(
    section_text: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Recursively split an oversized section.

    Strategy: paragraphs (\\n\\n) -> single newlines -> sentence boundaries.
    Pack small units greedily until the target is hit, then start a new chunk
    with `overlap_tokens` of context from the previous chunk.
    """
    if count_tokens(section_text) <= target_tokens:
        return [section_text]

    units = _split_by_delimiter(section_text, "\n\n")
    if len(units) == 1:
        units = _split_by_delimiter(section_text, "\n")
    if len(units) == 1:
        units = [s.strip() for s in _SENTENCE_SPLIT.split(section_text) if s.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)

        if unit_tokens > target_tokens:
            # A single unit is still too big (huge paragraph). Recurse by sentence.
            sub = [s.strip() for s in _SENTENCE_SPLIT.split(unit) if s.strip()]
            if len(sub) > 1:
                for s in sub:
                    s_tokens = count_tokens(s)
                    if current and current_tokens + s_tokens > target_tokens:
                        chunks.append("\n\n".join(current))
                        tail = _tail_by_tokens("\n\n".join(current), overlap_tokens)
                        current = [tail] if tail else []
                        current_tokens = count_tokens(tail) if tail else 0
                    current.append(s)
                    current_tokens += s_tokens
                continue
            # Can't split further semantically — hard-split by token boundary
            # to stay under BGE's 512-token limit.
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_tokens = 0
            for sub in _hard_split_by_tokens(unit, target_tokens):
                chunks.append(sub)
            continue

        if current and current_tokens + unit_tokens > target_tokens:
            chunks.append("\n\n".join(current))
            tail = _tail_by_tokens("\n\n".join(current), overlap_tokens)
            current = [tail] if tail else []
            current_tokens = count_tokens(tail) if tail else 0

        current.append(unit)
        current_tokens += unit_tokens

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def _tail_by_tokens(text: str, n_tokens: int) -> str:
    if n_tokens <= 0 or not text:
        return ""
    enc = _encoding()
    ids = enc.encode(text)
    if len(ids) <= n_tokens:
        return text
    return enc.decode(ids[-n_tokens:])


def _hard_split_by_tokens(text: str, n_tokens: int) -> list[str]:
    """Last-resort token-boundary split.

    Used when a unit can't be split semantically (no sentence boundaries) but
    still exceeds target_tokens. Prevents BGE's 512-token hard limit from
    silently truncating content at embed time.
    """
    enc = _encoding()
    ids = enc.encode(text)
    if len(ids) <= n_tokens:
        return [text]
    return [enc.decode(ids[i : i + n_tokens]) for i in range(0, len(ids), n_tokens)]


def chunk_markdown(
    text: str,
    file_path: str,
    target_tokens: int = _DEFAULT_TARGET,
    overlap_tokens: int = _DEFAULT_OVERLAP,
    min_tokens: int = _DEFAULT_MIN,
) -> list[dict]:
    """Chunk a markdown document into a list of chunk dicts.

    Returns: list of {"text": str, "heading_path": str, "chunk_index": int}.

    Strategy:
      1. Try heading-aware section splitting (the plan's default). This keeps
         multi-topic medium files (like SOUL.md — 374 tokens across 5 distinct
         sections) from being averaged into a single embedding that dilutes
         topic-specific queries.
      2. If section-splitting produces zero usable chunks because every
         section is below min_tokens (typical for people-profile stubs with
         "(to be filled)" placeholders), fall back to a single whole-file
         chunk with the filename as heading_path. Prevents those files from
         disappearing from the index entirely.
    """
    body = strip_frontmatter(text).strip()
    filename = os.path.basename(file_path)

    if not body:
        return []

    total = count_tokens(body)
    if total < min_tokens:
        return []

    # Path 1: section-aware chunking.
    sections = split_sections(body, filename)
    chunks: list[dict] = []
    for section in sections:
        pieces = split_oversized(section["body"], target_tokens, overlap_tokens)
        for piece in pieces:
            if count_tokens(piece) < min_tokens:
                continue
            chunks.append(
                {
                    "text": piece,
                    "heading_path": section["heading_path"],
                    "chunk_index": len(chunks),
                }
            )

    # Path 2: whole-file fallback for tiny stubs (every section too small).
    if not chunks:
        return [{"text": body, "heading_path": filename, "chunk_index": 0}]

    return chunks


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print("usage: chunking.py <markdown-file>", file=__import__("sys").stderr)
        sys.exit(2)
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    result = chunk_markdown(text, path)
    print(f"{len(result)} chunks")
    for c in result:
        tk = count_tokens(c["text"])
        print(f"  [{c['chunk_index']}] {c['heading_path'][:70]} ({tk}t)")


if __name__ == "__main__":
    main()
