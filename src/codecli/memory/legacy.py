"""legacy.py — Read-only access to the retired SQLite long-term memory.

The CodeCLI memory system is now Claude Code style Markdown Auto Memory and
no longer depends on SQLite at runtime. This module exists only for the
one-time ``/memory migrate`` path: it reads the old ``~/.codecli/memory.db``
(read-only, never writes) so existing memories can be imported into the
Markdown memory directory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_LEGACY_DB = "~/.codecli/memory.db"

_COLUMNS = """
    id, scope, content, created_at, kind, source, importance, confidence,
    updated_at, expires_at, access_count
"""


def legacy_db_default() -> str:
    return DEFAULT_LEGACY_DB


def read_legacy_memories(
    db_path: str | Path,
    *,
    scope: str | None = None,
    limit: int = 2_000,
) -> list[dict[str, Any]]:
    """Read rows from the legacy SQLite memory database without modifying it.

    Returns a list of plain dicts (id/content/kind/source/importance/
    confidence/created_at/updated_at). Missing or incompatible databases
    yield an empty list.
    """
    path = Path(db_path).expanduser()
    if not path.is_file():
        return []
    normalized_limit = max(int(limit), 0)
    if normalized_limit == 0:
        return []
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except (sqlite3.Error, OSError):
        return []
    try:
        conn.execute("pragma busy_timeout = 5000")
        params: list[Any] = []
        scope_clause = ""
        if scope is not None:
            scope_clause = " where scope = ?"
            params.append(str(scope))
        params.append(normalized_limit)
        rows = conn.execute(
            f"select {_COLUMNS} from memories{scope_clause} "
            "order by updated_at desc, id desc limit ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return [
        {
            "id": int(row["id"]),
            "scope": str(row["scope"]),
            "content": str(row["content"]),
            "created_at": str(row["created_at"]),
            "kind": str(row["kind"]),
            "source": str(row["source"]),
            "importance": float(row["importance"]),
            "confidence": float(row["confidence"]),
            "updated_at": str(row["updated_at"]),
            "expires_at": str(row["expires_at"]) if row["expires_at"] is not None else None,
            "access_count": int(row["access_count"]),
        }
        for row in rows
    ]


__all__ = ["DEFAULT_LEGACY_DB", "legacy_db_default", "read_legacy_memories"]
