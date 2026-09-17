"""FastEmbed wrapper for BAAI/bge-small-en-v1.5 (384-dim).

BGE v1.5 is trained without an instruction prefix on either side — queries and
documents are embedded symmetrically. Do NOT prepend "query: " (that's an E5
convention).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
from fastembed import TextEmbedding

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import get_project_root  # noqa: E402

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_DIM = 384

_embedder: Optional[TextEmbedding] = None


def _cache_dir() -> str:
    return str(get_project_root() / ".fastembed_cache")


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(
            model_name=_MODEL_NAME,
            cache_dir=_cache_dir(),
        )
    return _embedder


def embed_docs(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, _DIM), dtype=np.float32)
    return np.vstack(list(get_embedder().embed(texts, batch_size=64)))


def embed_query(text: str) -> np.ndarray:
    return embed_docs([text])[0]


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: embeddings.py <text>", file=sys.stderr)
        sys.exit(2)
    vec = embed_query(sys.argv[1])
    print(f"shape={vec.shape} first8={vec[:8].tolist()}")


if __name__ == "__main__":
    main()
