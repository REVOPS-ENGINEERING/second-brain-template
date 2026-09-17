"""Incremental indexer for Vault/Memory.

Walks the vault, compares mtimes, re-chunks/re-embeds changed files, writes to
both chunks_vec and chunks_fts (shared rowid). Single commit at the end.

Usage:
    python .claude/scripts/memory_index.py [--full] [--verbose]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chunking import chunk_markdown  # noqa: E402
from db import (  # noqa: E402
    connect,
    default_db_path,
    drop_tables,
    get_all_file_paths,
    migrate,
    serialize_vector,
)
from embeddings import embed_docs  # noqa: E402
from shared import file_lock, get_project_root, get_vault_root  # noqa: E402


def list_markdown_files(root: Path) -> list[Path]:
    """List all .md files under root, excluding any 'trash' component.

    Soft-deleted files in Vault/Memory/trash/ should never surface in
    semantic search. Once the walker skips them, the orphan-cleanup pass at
    the bottom of main() will drop their existing chunks on the next reindex,
    so no migration is needed when this filter is first introduced.
    """
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*.md")
        if "trash" not in p.relative_to(root).parts
    )


def file_current_mtime(conn, file_path: str) -> float | None:
    row = conn.execute(
        "SELECT mtime FROM chunks_vec WHERE file_path = ? LIMIT 1",
        (file_path,),
    ).fetchone()
    return row[0] if row else None


def file_needs_reindex(conn, file_path: str, mtime: float) -> bool:
    existing = file_current_mtime(conn, file_path)
    if existing is None:
        return True
    return abs(existing - mtime) > 1e-6


def delete_file_chunks(conn, file_path: str) -> None:
    conn.execute("DELETE FROM chunks_vec WHERE file_path = ?", (file_path,))
    conn.execute("DELETE FROM chunks_fts WHERE file_path = ?", (file_path,))


def index_file(
    conn, file_path: Path, project_root: Path, verbose: bool = False
) -> int:
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        print(f"  WARN: skipping {file_path}: {e}", file=sys.stderr)
        return 0

    mtime = file_path.stat().st_mtime
    # Store a stable project-relative path so queries can match across cwds.
    try:
        rel = str(file_path.resolve().relative_to(project_root))
    except ValueError:
        rel = str(file_path)

    chunks = chunk_markdown(text, rel)
    # Wipe any prior chunks for this file before deciding whether we have new
    # ones to insert — otherwise a file that gets emptied in place (content
    # deleted, only frontmatter left) would keep its stale chunks forever.
    delete_file_chunks(conn, rel)
    if not chunks:
        if verbose:
            print(f"  - {rel}: 0 chunks (empty/frontmatter-only)", file=sys.stderr)
        return 0

    # Fold heading_path ("filename > h1 > h2") into the EMBEDDED text so the
    # vector carries the chunk's structural context — a body that says "we
    # decided X" embeds nowhere near "garage renovation" without its heading.
    # Only the embed input changes: the stored `text` column and the FTS insert
    # below stay body-only (chunks_fts already indexes heading_path as its own
    # column, so literal heading terms have always retrieved via keyword search
    # — this is a semantic-recall fix, and a tradeoff: heading text can dilute
    # recall for body-specific queries). Needs a `--full` reindex to take
    # effect on existing chunks.
    texts = [f"{c['heading_path']}\n\n{c['text']}" for c in chunks]
    vectors = embed_docs(texts)

    for c, vec in zip(chunks, vectors):
        cur = conn.execute(
            """
            INSERT INTO chunks_vec
                (embedding, file_path, chunk_index, mtime, heading_path, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                serialize_vector(vec),
                rel,
                c["chunk_index"],
                mtime,
                c["heading_path"],
                c["text"],
            ),
        )
        rowid = cur.lastrowid
        conn.execute(
            """
            INSERT INTO chunks_fts (rowid, text, heading_path, file_path)
            VALUES (?, ?, ?, ?)
            """,
            (rowid, c["text"], c["heading_path"], rel),
        )

    if verbose:
        print(f"  + {rel}: {len(chunks)} chunks", file=sys.stderr)
    return len(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Vault/Memory into memory.db")
    parser.add_argument(
        "--full",
        action="store_true",
        help="drop and rebuild both tables from scratch",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print per-file progress to stderr",
    )
    args = parser.parse_args()

    t0 = time.perf_counter()

    project_root = get_project_root()
    vault_root = get_vault_root()
    db_path = default_db_path()

    # shared.file_lock appends ".lock" to whatever path we pass — so pass the
    # DB path itself, not a pre-suffixed one, to get a sibling .lock file.
    with file_lock(db_path):
        conn = connect(path=db_path)
        try:
            if args.full:
                drop_tables(conn)
            migrate(conn)

            files = list_markdown_files(vault_root)
            if not files:
                print(f"No markdown files found under {vault_root}", file=sys.stderr)
                return

            total_chunks = 0
            indexed_files = 0
            unchanged_files = 0
            on_disk_paths: set[str] = set()

            for md in files:
                mtime = md.stat().st_mtime
                try:
                    rel = str(md.resolve().relative_to(project_root))
                except ValueError:
                    rel = str(md)
                on_disk_paths.add(rel)
                if not args.full and not file_needs_reindex(conn, rel, mtime):
                    unchanged_files += 1
                    if args.verbose:
                        print(f"  = {rel}: unchanged", file=sys.stderr)
                    continue
                total_chunks += index_file(
                    conn, md, project_root=project_root, verbose=args.verbose
                )
                indexed_files += 1

            # Stale-file cleanup: any path in the DB that's no longer on disk
            # gets its chunks dropped from both chunks_vec and chunks_fts. Must
            # happen inside the same transaction as the indexing writes above.
            indexed_paths = set(get_all_file_paths(conn))
            orphans = indexed_paths - on_disk_paths
            for orphan in sorted(orphans):
                delete_file_chunks(conn, orphan)
                if args.verbose:
                    print(
                        f"  - {orphan}: removed (file no longer on disk)",
                        file=sys.stderr,
                    )
            orphan_count = len(orphans)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    elapsed = time.perf_counter() - t0
    if args.verbose and unchanged_files:
        print(f"unchanged {unchanged_files} files", file=sys.stderr)
    print(
        f"indexed {total_chunks} chunks across {indexed_files} files, "
        f"removed {orphan_count} stale files in {elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
