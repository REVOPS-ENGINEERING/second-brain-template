#!/usr/bin/env python3
"""Discover recent work with zero LLM tokens.

Two passes:
1. Claude Code history — scans ~/.claude/projects/, resolves each project's
   real path from the `cwd` field inside its transcript .jsonl files, counts
   recent sessions.
2. Git scan — walks code roots (inferred from the parents of the Claude Code
   project paths, plus any --roots given) for ANY git repo with commits in the
   window, catching work done in other tools (Cursor, other agents, editors).

Noise is filtered deterministically in-script: /tmp checkouts, other agent
tools' checkout dirs and worktrees, and directories that no longer exist are
dropped, and only the top 15 by recency are kept. Pass --all to skip all
filtering. Git worktrees detected via `git rev-parse --git-common-dir` are
kept but annotated with "worktree_of" so they can be discussed alongside
their parent repo rather than treated as separate projects.

Emits compact JSON. Reads transcript metadata only (first few lines per
file) — never message content, so nothing sensitive is printed.

Usage:
    discover.py [DAYS] [--roots DIR ...] [--all]   # window default 30 days
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
MAX_HEAD_LINES = 25  # cwd appears in the first few records of a transcript
KEEP_TOP = 15


def resolve_cwd(project_dir: Path) -> str | None:
    """Pull the real project path from the newest transcripts' cwd field."""
    jsonls = sorted(
        project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for f in jsonls[:3]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= MAX_HEAD_LINES:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
        except OSError:
            continue
    return None


def git_summary(repo: Path, since_days: int) -> tuple[int, list[str]]:
    """(commit_count, last 3 subject lines) within the window; (0, []) if not a repo."""
    if not (repo / ".git").exists():
        return 0, []
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={since_days} days ago",
             "--pretty=%ad %s", "--date=short"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return 0, []
    lines = [l for l in out.splitlines() if l.strip()]
    return len(lines), lines[:3]


def worktree_parent(repo: Path) -> str | None:
    """If repo is a linked git worktree, return the parent repo path."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
    except (subprocess.TimeoutExpired, OSError):
        return None
    if len(out) != 2:
        return None
    git_dir = Path(out[0]) if Path(out[0]).is_absolute() else (repo / out[0])
    common = Path(out[1]) if Path(out[1]).is_absolute() else (repo / out[1])
    if git_dir.resolve() == common.resolve():
        return None  # normal repo, not a linked worktree
    return str(common.resolve().parent)


SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__",
             ".cache", "dist", "build", ".archon", "worktrees"}


def find_git_repos(roots: list[Path], max_depth: int = 3) -> list[Path]:
    """Find git repo top-levels under roots, shallow walk, pruned."""
    import os
    found: list[Path] = []
    for root in roots:
        root = root.resolve()
        if not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root):
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if ".git" in dirnames:
                found.append(Path(dirpath))
                dirnames.clear()  # don't descend into a repo
                continue
            if rel_depth >= max_depth:
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
    return found


def noise_reason(row: dict) -> str | None:
    """Why a row should be filtered out, or None to keep it."""
    p = row["path"]
    if p == "/tmp" or p.startswith("/tmp/"):
        return "tmp-scratch"
    if not row["exists"]:
        return "directory-missing"
    parts = Path(p).parts
    if ".archon" in parts or "worktrees" in parts:
        return "worktree-checkout"
    return None


def main() -> None:
    args = sys.argv[1:]
    show_all = "--all" in args
    if show_all:
        args.remove("--all")
    extra_roots: list[Path] = []
    if "--roots" in args:
        i = args.index("--roots")
        extra_roots = [Path(p).expanduser() for p in args[i + 1:]]
        args = args[:i]
    days = int(args[0]) if args else 30
    cutoff = datetime.now() - timedelta(days=days)
    here = Path.cwd().resolve()

    if not CLAUDE_PROJECTS.is_dir() and not extra_roots:
        print(json.dumps({"error": f"No Claude Code project history at "
                          f"{CLAUDE_PROJECTS} and no --roots given."}))
        return

    rows = []
    seen: set[str] = set()

    # Pass 1: Claude Code session history
    for pdir in CLAUDE_PROJECTS.iterdir() if CLAUDE_PROJECTS.is_dir() else []:
        if not pdir.is_dir():
            continue
        transcripts = list(pdir.glob("*.jsonl"))
        if not transcripts:
            continue
        recent = [t for t in transcripts
                  if datetime.fromtimestamp(t.stat().st_mtime) >= cutoff]
        if not recent:
            continue
        cwd = resolve_cwd(pdir)
        if cwd is None:
            continue
        cwd_path = Path(cwd)
        if cwd_path.resolve() == here:
            continue  # skip the second-brain repo itself
        last_session = datetime.fromtimestamp(
            max(t.stat().st_mtime for t in recent)
        )
        newest = sorted(recent, key=lambda p: p.stat().st_mtime, reverse=True)
        commits, subjects = git_summary(cwd_path, days)
        seen.add(str(cwd_path.resolve()) if cwd_path.exists() else cwd)
        rows.append({
            "path": cwd,
            "source": "claude-code",
            "exists": cwd_path.is_dir(),
            "sessions": len(recent),
            "last_active": last_session.strftime("%Y-%m-%d"),
            "transcripts": [str(t) for t in newest[:3]],
            "commits": commits,
            "recent_commits": subjects,
        })

    # Pass 2: git scan over inferred + explicit roots
    inferred = {Path(r["path"]).parent for r in rows if r["exists"]}
    roots = [r for r in {*inferred, *extra_roots} if r.is_dir()]
    for repo in find_git_repos(roots):
        key = str(repo.resolve())
        if key in seen or repo.resolve() == here:
            continue
        seen.add(key)
        commits, subjects = git_summary(repo, days)
        if commits == 0:
            continue
        last_commit = subjects[0].split()[0] if subjects else "?"
        rows.append({
            "path": str(repo),
            "source": "git-only",
            "exists": True,
            "sessions": 0,
            "last_active": last_commit,
            "transcripts": [],
            "commits": commits,
            "recent_commits": subjects,
        })

    # Annotate linked git worktrees (kept, not merged into the parent)
    for r in rows:
        if r["exists"]:
            parent = worktree_parent(Path(r["path"]))
            if parent:
                r["worktree_of"] = parent

    rows.sort(key=lambda r: r["last_active"], reverse=True)

    filtered_out: list[dict] = []
    if not show_all:
        kept = []
        for r in rows:
            reason = noise_reason(r)
            if reason:
                filtered_out.append({"path": r["path"], "reason": reason})
            else:
                kept.append(r)
        overflow = kept[KEEP_TOP:]
        for r in overflow:
            filtered_out.append({"path": r["path"], "reason": "beyond-top-15"})
        rows = kept[:KEEP_TOP]

    out = {
        "window_days": days,
        "projects": rows,
        "filtered_out": filtered_out,
        "note": "" if show_all else
                "Noise pre-filtered (tmp, worktree checkouts, dead dirs, "
                "top 15 by recency). Re-run with --all to see everything.",
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
