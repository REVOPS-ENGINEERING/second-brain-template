"""SQLite + sqlite-vec + FTS5 backend for memory search.

vec0 holds embeddings with filterable metadata columns (file_path,
chunk_index, mtime) plus auxiliary columns (+heading_path, +text). FTS5
mirrors text/heading_path/file_path for BM25 keyword search. Both tables
share the same rowid, managed by the indexer.

A Postgres backend is stubbed behind MEMORY_DB=postgres (not yet implemented).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import get_project_root  # noqa: E402

VECTOR_DIM = 384


def default_db_path() -> str:
    return str(get_project_root() / ".claude" / "data" / "memory.db")


def connect_sqlite(path: Optional[str] = None) -> sqlite3.Connection:
    if path is None:
        path = default_db_path()
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect(
    backend: Optional[str] = None, path: Optional[str] = None
) -> sqlite3.Connection:
    backend = backend or os.environ.get("MEMORY_DB", "sqlite")
    if backend == "sqlite":
        return connect_sqlite(path)
    if backend == "postgres":
        raise NotImplementedError("Postgres backend: not yet implemented")
    raise ValueError(f"unknown MEMORY_DB backend: {backend}")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> None:
    """Create vec0 and FTS5 tables if absent. Idempotent."""
    if not _table_exists(conn, "chunks_vec"):
        try:
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE chunks_vec USING vec0(
                    id INTEGER PRIMARY KEY,
                    embedding float[{VECTOR_DIM}],
                    file_path TEXT,
                    chunk_index INTEGER,
                    mtime FLOAT,
                    +heading_path TEXT,
                    +text TEXT
                )
                """
            )
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e).lower():
                raise

    if not _table_exists(conn, "chunks_fts"):
        conn.execute(
            """
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                text, heading_path, file_path,
                tokenize = 'porter unicode61'
            )
            """
        )

    conn.commit()


def drop_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS chunks_vec")
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.commit()


def get_all_file_paths(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT DISTINCT file_path FROM chunks_vec")]


def serialize_vector(vec: np.ndarray) -> bytes:
    return sqlite_vec.serialize_float32(vec.astype(np.float32).tolist())


def main() -> None:
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    conn = connect(path=path)
    migrate(conn)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
        ).fetchall()
    ]
    print("tables:", tables)


if __name__ == "__main__":
    main()
