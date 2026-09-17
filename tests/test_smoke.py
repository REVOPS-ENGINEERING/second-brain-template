"""Smoke test: the ported module set imports and its core primitives work.

Not a full test suite (the code is battle-tested upstream) — just a regression
net so future edits that break imports or basic behavior fail fast.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".claude" / "scripts"
HOOKS = REPO_ROOT / ".claude" / "hooks"
sys.path.insert(0, str(SCRIPTS))


def test_all_modules_import():
    import chunking  # noqa: F401
    import db  # noqa: F401
    import embeddings  # noqa: F401
    import hook_helpers  # noqa: F401
    import memory_flush  # noqa: F401
    import memory_index  # noqa: F401
    import memory_reflect  # noqa: F401
    import memory_search  # noqa: F401
    import shared  # noqa: F401


def test_hooks_compile():
    import py_compile

    for hook in HOOKS.glob("*.py"):
        py_compile.compile(str(hook), doraise=True)


def test_project_root_resolves(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    import shared

    assert shared.get_project_root() == REPO_ROOT


def test_vault_root(monkeypatch):
    monkeypatch.delenv("BRAIN_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    import shared

    assert shared.get_vault_root() == REPO_ROOT / "Vault" / "Memory"


def test_chunk_markdown():
    import chunking

    doc = "# Title\n\nSome intro text.\n\n## Section A\n\n" + ("Sentence about topic A. " * 30) + "\n\n## Section B\n\nShort note."
    chunks = chunking.chunk_markdown(doc, "sample.md")
    assert len(chunks) >= 1
    assert all(c["text"].strip() for c in chunks)
    assert all("heading_path" in c and "chunk_index" in c for c in chunks)


def test_vector_roundtrip():
    import db
    import numpy as np

    vec = np.random.rand(384).astype(np.float32)
    blob = db.serialize_vector(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == 384 * 4
    back = np.frombuffer(blob, dtype=np.float32)
    assert np.allclose(vec, back)
