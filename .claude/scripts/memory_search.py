"""Hybrid vector + keyword search over memory.db.

Runs the query through sqlite-vec KNN and FTS5 in parallel, merges with
Reciprocal Rank Fusion (k=60), prints the top-k results.

Usage:
    python .claude/scripts/memory_search.py "query" [--k N] [--path-prefix PREFIX] [--min-score F]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from db import connect, serialize_vector  # noqa: E402
from embeddings import embed_query  # noqa: E402

RRF_K = 60


def _normalize_prefix(prefix: str) -> str:
    p = prefix.strip()
    if not p.startswith("Vault/Memory/"):
        p = f"Vault/Memory/{p.strip('/')}"
    if not p.endswith("/"):
        p += "/"
    return p


def _prefix_pattern(prefix: str | None) -> str | None:
    if not prefix:
        return None
    return f"{_normalize_prefix(prefix)}%"


def _prefix_range(prefix: str) -> tuple[str, str]:
    """Return (lower_inclusive, upper_exclusive) for a vec0-safe range scan."""
    lo = _normalize_prefix(prefix)
    hi = lo[:-1] + chr(ord(lo[-1]) + 1)
    return lo, hi


def vec_search(conn, qvec: np.ndarray, k: int, path_prefix: str | None) -> list[dict]:
    if path_prefix:
        lo, hi = _prefix_range(path_prefix)
        sql = """
            SELECT id, file_path, chunk_index, heading_path, text, distance
            FROM chunks_vec
            WHERE embedding MATCH ?
              AND k = ?
              AND file_path >= ?
              AND file_path < ?
            ORDER BY distance
        """
        params: tuple = (serialize_vector(qvec), k, lo, hi)
    else:
        sql = """
            SELECT id, file_path, chunk_index, heading_path, text, distance
            FROM chunks_vec
            WHERE embedding MATCH ?
              AND k = ?
            ORDER BY distance
        """
        params = (serialize_vector(qvec), k)

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "file_path": r[1],
            "chunk_index": r[2],
            "heading_path": r[3],
            "text": r[4],
            "distance": r[5],
        }
        for r in rows
    ]


# FTS5 operator/special chars to strip before quoting. Stripping (not escaping)
# is fine because we're doing keyword search, not trying to preserve punctuation.
_FTS_OPS = re.compile(r'[\"():*^\-]+')


def _fts_sanitize(query: str) -> str:
    """Turn a free-form user query into a safe FTS5 OR-of-terms query.

    Strips FTS5 operator chars, splits on whitespace, wraps each token in
    double quotes (so accidentally reserved words like AND/OR/NOT are treated
    as literals), joins with OR for broad recall. BM25 ranks the results.
    """
    cleaned = _FTS_OPS.sub(" ", query)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'  # matches nothing
    return " OR ".join(f'"{t}"' for t in tokens)


def fts_search(conn, query: str, k: int, path_prefix: str | None) -> list[dict]:
    pattern = _prefix_pattern(path_prefix)
    fts_query = _fts_sanitize(query)

    if pattern:
        sql = """
            SELECT rowid, file_path, heading_path, text, bm25(chunks_fts) AS rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
              AND file_path LIKE ?
            ORDER BY rank
            LIMIT ?
        """
        params: tuple = (fts_query, pattern, k)
    else:
        sql = """
            SELECT rowid, file_path, heading_path, text, bm25(chunks_fts) AS rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        params = (fts_query, k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # Only FTS5 query-syntax errors should be swallowed here; anything
        # else (schema mismatch, programming errors) should propagate.
        print(f"fts warning: {e}", file=sys.stderr)
        return []

    return [
        {
            "id": r[0],
            "file_path": r[1],
            "heading_path": r[2],
            "text": r[3],
            "rank": r[4],
        }
        for r in rows
    ]


def rrf_merge(
    vec_hits: list[dict],
    fts_hits: list[dict],
    k: int,
    min_score: float | None = None,
) -> list[dict]:
    scores: dict[int, dict] = {}

    def _add(hits: list[dict]) -> None:
        for rank, hit in enumerate(hits):
            slot = scores.setdefault(hit["id"], {**hit, "score": 0.0})
            slot["score"] += 1.0 / (RRF_K + rank + 1)

    _add(vec_hits)
    _add(fts_hits)

    ranked = sorted(scores.values(), key=lambda x: -x["score"])
    if min_score is not None:
        ranked = [r for r in ranked if r["score"] >= min_score]
    return ranked[:k]


def format_result(r: dict) -> str:
    text = r["text"].replace("\n", " ").strip()
    if len(text) > 120:
        text = text[:117] + "..."
    heading = r.get("heading_path") or ""
    return f"[score {r['score']:.4f}] {r['file_path']}:{heading}\n    {text}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid search over Vault memory.")
    parser.add_argument("query", help="query string")
    parser.add_argument("--k", type=int, default=10, help="top-k results (default 10)")
    parser.add_argument(
        "--path-prefix",
        default=None,
        help="restrict to files under Vault/Memory/<prefix>",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="drop results with RRF score below this floor (default: no filter)",
    )
    args = parser.parse_args()

    conn = connect()
    try:
        qvec = embed_query(args.query)
        fan_out = max(args.k * 2, 10)
        vec_hits = vec_search(conn, qvec, fan_out, args.path_prefix)
        fts_hits = fts_search(conn, args.query, fan_out, args.path_prefix)
        merged = rrf_merge(vec_hits, fts_hits, args.k, min_score=args.min_score)
    finally:
        conn.close()

    if not merged:
        print("(no results)")
        return

    for r in merged:
        print(format_result(r))


if __name__ == "__main__":
    main()
