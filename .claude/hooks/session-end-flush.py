#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

# Note: recursion guard (CLAUDE_INVOKED_BY check) is inside spawn_flush() so
# that skip path is captured in hook-execution.log. Worth the trivial import cost.

_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
if _project_dir:
    sys.path.insert(0, str(Path(_project_dir) / ".claude" / "scripts"))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hook_helpers import spawn_flush  # noqa: E402


if __name__ == "__main__":
    sys.exit(spawn_flush())
