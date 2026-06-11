"""Shared exclude-pattern matching for watcher, GC, and migration.

Pattern semantics (all matched against workspace-root-relative paths):

* ``"name/"``           — directory *component* match anywhere in the path
                          (``node_modules/`` excludes any node_modules dir).
                          Globs allowed (``.venv*/``).
* ``"a/b/c/"``          — directory *path prefix* match from the workspace
                          root (``e2esc/local-test-data/`` excludes that
                          subtree only). Globs allowed per component.
* ``"*.pyc"``           — basename glob (any file named like this anywhere).
* ``"a/b/*.csv"``       — full-relative-path glob.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import Sequence


def _norm(rel_path: str) -> str:
    """Normalise to forward slashes, no leading './'."""
    p = rel_path.replace("\\", "/").lstrip("/")
    return p[2:] if p.startswith("./") else p


def is_excluded(rel_path: str, patterns: Sequence[str]) -> bool:
    """Return True if *rel_path* (relative to the watch root) matches any pattern."""
    rel = _norm(rel_path)
    parts = PurePosixPath(rel).parts
    for raw in patterns:
        pattern = _norm(raw)
        if pattern.endswith("/"):
            pattern = pattern.rstrip("/")
            if "/" in pattern:
                # Path-prefix directory pattern: match leading components.
                pat_parts = PurePosixPath(pattern).parts
                if len(parts) > len(pat_parts) or (
                    len(parts) == len(pat_parts)
                ):
                    if len(parts) >= len(pat_parts) and all(
                        fnmatch.fnmatch(p, q) for p, q in zip(parts, pat_parts)
                    ):
                        return True
            else:
                # Component pattern: any directory component matches.
                if any(fnmatch.fnmatch(p, pattern) for p in parts):
                    return True
        elif "/" in pattern:
            # Full relative-path glob.
            if fnmatch.fnmatch(rel, pattern):
                return True
        else:
            # Basename glob.
            if fnmatch.fnmatch(parts[-1] if parts else rel, pattern):
                return True
    return False


def dir_is_excluded(rel_dir: str, patterns: Sequence[str]) -> bool:
    """True if the directory itself is excluded (used to prune tree walks).

    A directory is prunable when a component pattern matches one of its
    components, or a path-prefix pattern matches the directory at or above it.
    """
    rel = _norm(rel_dir)
    parts = PurePosixPath(rel).parts
    for raw in patterns:
        pattern = _norm(raw)
        if not pattern.endswith("/"):
            continue
        pattern = pattern.rstrip("/")
        if "/" in pattern:
            pat_parts = PurePosixPath(pattern).parts
            if len(parts) >= len(pat_parts) and all(
                fnmatch.fnmatch(p, q) for p, q in zip(parts, pat_parts)
            ):
                return True
        else:
            if any(fnmatch.fnmatch(p, pattern) for p in parts):
                return True
    return False
