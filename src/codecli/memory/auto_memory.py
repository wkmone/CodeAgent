"""auto_memory.py — Claude Code style Markdown Auto Memory.

Implements the storage engine that was verified against the leaked Claude Code
v2.1.88 source (utils/memory + src/memdir):

- Project directory derived from the sanitized git root (``~/.codecli/memory/
  projects/-Users-me-myproject/memory/`` style), shared across worktrees.
- ``MEMORY.md`` index (<= 200 lines / 25KB, <= 150 chars per line) + topic
  files with ``name`` / ``description`` / ``type`` / ``scope`` frontmatter.
- Two-step writes: topic file first, then the MEMORY.md index line.
- Atomic writes (tempfile + os.replace) with a cross-process lock and
  BOM / CRLF normalization, so concurrent CLI / server / worker processes do
  not corrupt the directory.
- Recall via a per-user-turn LLM side-call that selects up to
  ``recall_max_files`` topic files from a manifest (filename + type +
  description + modified timestamp), with a drift/freshness warning for
  memories older than a day.

The model writes memories with the *generic* file tools (write_file /
edit_file / glob), never with dedicated memory tools — mirroring Claude Code's
``createAutoMemCanUseTool`` scoping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from codecli.prompt.memory_prompts import SELECT_MEMORIES_SYSTEM_PROMPT
from codecli.types import Message

# ---------------------------------------------------------------------------
# Limits (aligned with the leaked implementation)
# ---------------------------------------------------------------------------

DEFAULT_MAX_INDEX_LINES = 200
DEFAULT_MAX_INDEX_BYTES = 25 * 1024  # 25KB
DEFAULT_MAX_INDEX_LINE_CHARS = 150
DEFAULT_MAX_SELECTED_FILES = 5
FRESHNESS_AFTER_DAYS = 1
INDEX_HEADING = "# Memory Index"
_LOCK_FILE = ".memory.lock"
_FRONTMETTER_RE = re.compile(r"^\s*---\s*$(.*?)^\s*---\s*$", re.MULTILINE | re.DOTALL)
_SLUG_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]+")
_DUPS_DASH_RE = re.compile(r"-{2,}")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryHeader:
    """Manifest entry for one topic file."""

    filename: str
    name: str
    type: str = "project"
    description: str = ""
    scope: str = "private"
    modified_ts: str = ""

    def to_manifest_line(self) -> str:
        desc = self.description or (self.name or Path(self.filename).stem)
        return f"- [{self.type}] {self.filename} ({self.modified_ts}): {desc}"


@dataclass(slots=True)
class MemoryFile:
    header: MemoryHeader
    body: str
    freshness: str = ""  # memoryFreshnessText() equivalent, if memory is old


# ---------------------------------------------------------------------------
# Path / project identity
# ---------------------------------------------------------------------------


@lru_cache(maxsize=128)
def _git_root(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return root or None


def sanitize_project_slug(cwd: str) -> str:
    """Return a stable, human-readable slug for *cwd*.

    Prefers the git root (all worktrees of one repo share memory, matching
    Claude Code), otherwise the absolute cwd path. Windows drive letters and
    path separators are normalized, e.g. ``D:\\Code\\think\\CodeAgent`` →
    ``-D-Code-think-CodeAgent``.
    """
    root = _git_root(cwd) or os.path.abspath(cwd)
    normalized = root.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part and part not in (".", "..")]
    slug_parts: list[str] = []
    for part in parts:
        cleaned = _SLUG_CHARS_RE.sub("-", part).strip("-")
        slug_parts.append(cleaned or "_")
    slug = _DUPS_DASH_RE.sub("-", "-".join(slug_parts)).strip("-")
    return f"-{slug or 'workspace'}"


def memory_dir_for(root: str, cwd: str) -> Path:
    """Resolve ``<root>/projects/<slug>/memory`` for *cwd*."""
    return Path(root).expanduser() / "projects" / sanitize_project_slug(cwd) / "memory"


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split *text* into (frontmatter dict, body). Missing/invalid frontmatter
    yields an empty dict and the original text as the body."""
    match = _FRONTMETTER_RE.search(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end() :].lstrip("\n")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            fields[key.strip().lower()] = value.strip()
    return fields, body


def format_frontmatter(name: str, description: str, type_: str, scope: str = "private") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {type_}\n"
        f"scope: {scope}\n"
        "---\n\n"
    )


def _coerce_memory_type(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"user", "feedback", "project", "reference"} else "project"


def _coerce_scope(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"private", "team"} else "private"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def memory_freshness_text(modified_iso: str) -> str:
    """memoryFreshnessText() equivalent: warn when a memory is >1 day old."""
    parsed = _parse_iso(modified_iso)
    if not parsed:
        return ""
    age = datetime.now(UTC) - parsed
    if age <= timedelta(days=FRESHNESS_AFTER_DAYS):
        return ""
    days = max(int(age.total_seconds() // 86_400), 1)
    return (
        f"\n\n> This memory is {days} days old. Memories are point-in-time "
        "observations, not live state. Claims about code behavior or file "
        "references may be outdated — verify against the current project "
        "before acting on it.\n"
    )


# ---------------------------------------------------------------------------
# Atomic file writes with a cross-process lock
# ---------------------------------------------------------------------------


# In-process fallback locks keyed by lock-file path (used only when neither
# msvcrt (Windows) nor fcntl (POSIX) is importable in this interpreter).
_INPROCESS_LOCKS: dict[str, threading.Lock] = {}
_INPROCESS_GUARD = threading.Lock()


class _CrossProcessLock:
    """Best-effort cross-process lock via msvcrt (Windows) / fcntl (POSIX).

    Acquisition is non-blocking + bounded retry: a contender spins until
    *timeout* seconds elapse, then raises TimeoutError instead of silently
    running without a lock. Threads within the same process serialize through
    the same file lock; where neither msvcrt nor fcntl is available, an
    in-process threading.Lock (per path) backs the same contract.
    """

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: Any = None
        self._thread_lock: threading.Lock | None = None

    def __enter__(self) -> "_CrossProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            deadline = time.monotonic() + self.timeout
            if os.name == "nt":
                import msvcrt

                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"could not acquire memory lock {self.path} "
                                f"within {self.timeout}s"
                            )
                        time.sleep(0.05)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"could not acquire memory lock {self.path} "
                                f"within {self.timeout}s"
                            )
                        time.sleep(0.05)
            self._handle = handle
            return self
        except ImportError:
            # Neither msvcrt nor fcntl exists: fall back to an in-process lock
            # so same-process threads still serialize (docstring contract).
            handle.close()
            with _INPROCESS_GUARD:
                thread_lock = _INPROCESS_LOCKS.setdefault(str(self.path), threading.Lock())
            if not thread_lock.acquire(timeout=self.timeout):
                raise TimeoutError(
                    f"could not acquire in-process lock {self.path} "
                    f"within {self.timeout}s"
                )
            self._thread_lock = thread_lock
            return self
        except BaseException:
            handle.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._thread_lock is not None:
            self._thread_lock.release()
            self._thread_lock = None
            return
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def _normalize_text_for_write(text: str) -> str:
    """Strip a UTF-8 BOM and normalize CRLF to LF so memory files stay stable
    across platforms and edits."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_write_unlocked(path: Path, normalized: str) -> None:
    """Write *normalized* via tempfile + fsync + os.replace.

    Caller must already hold the sibling ``.memory.lock`` (via
    ``_CrossProcessLock`` or the ``already_locked`` path of ``atomic_write``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress_os_error():
            os.unlink(tmp_name)
        raise


def atomic_write(path: Path, text: str, *, already_locked: bool = False) -> None:
    """Write *text* atomically: tempfile + fsync + os.replace.

    *path* and its parent are created as needed. A cross-process lock on the
    sibling ``.memory.lock`` serializes concurrent writers (CLI / server /
    worker run in different processes).

    When *already_locked* is True the caller has already acquired the same
    lock (e.g. wrapping a read-modify-write transaction); the write proceeds
    without re-acquiring it (msvcrt/fcntl byte-range locks are not reentrant
    across a second handle).
    """
    lock_path = path.parent / _LOCK_FILE
    normalized = _normalize_text_for_write(text)
    if already_locked:
        _atomic_write_unlocked(path, normalized)
    else:
        with _CrossProcessLock(lock_path):
            _atomic_write_unlocked(path, normalized)


def suppress_os_error() -> Any:
    return suppress(OSError)


def read_text_stable(path: Path) -> str:
    """Read a memory file as UTF-8 with BOM handling; missing file → ""."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return _normalize_text_for_write(raw)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AutoMemoryStore:
    """The Markdown Auto Memory engine for one project (sanitized git root)."""

    def __init__(
        self,
        root: str | Path,
        cwd: str,
        *,
        max_index_lines: int = DEFAULT_MAX_INDEX_LINES,
        max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
        max_index_line_chars: int = DEFAULT_MAX_INDEX_LINE_CHARS,
        max_selected_files: int = DEFAULT_MAX_SELECTED_FILES,
    ) -> None:
        self.root = Path(root).expanduser()
        self.cwd = str(cwd)
        self.max_index_lines = max_index_lines
        self.max_index_bytes = max_index_bytes
        self.max_index_line_chars = max_index_line_chars
        self.max_selected_files = max_selected_files
        self.memory_dir = self.root / "projects" / sanitize_project_slug(self.cwd) / "memory"
        self.index_path = self.memory_dir / "MEMORY.md"

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        return self.memory_dir.is_dir() and any(
            self.memory_dir.rglob("*.md")
        )

    def scan(self) -> list[MemoryHeader]:
        """Scan topic files (excluding MEMORY.md) and parse frontmatter."""
        if not self.memory_dir.is_dir():
            return []
        headers: list[MemoryHeader] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            text = read_text_stable(path)
            fields, _body = parse_frontmatter(text)
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                modified_ts = mtime.isoformat(timespec="seconds")
            except OSError:
                modified_ts = ""
            headers.append(
                MemoryHeader(
                    filename=path.name,
                    name=str(fields.get("name") or Path(path.name).stem),
                    type=_coerce_memory_type(fields.get("type")),
                    description=str(fields.get("description") or ""),
                    scope=_coerce_scope(fields.get("scope")),
                    modified_ts=modified_ts,
                )
            )
        return headers

    def read_index(self) -> str:
        return read_text_stable(self.index_path)

    def read_topic(self, filename: str) -> MemoryFile | None:
        """Read one topic file; attach a freshness warning when it is old."""
        path = (self.memory_dir / filename).resolve()
        if not path.is_file() or path.parent != self.memory_dir.resolve():
            return None
        text = read_text_stable(path)
        fields, body = parse_frontmatter(text)
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            modified_ts = mtime.isoformat(timespec="seconds")
        except OSError:
            modified_ts = ""
        header = MemoryHeader(
            filename=path.name,
            name=str(fields.get("name") or Path(path.name).stem),
            type=_coerce_memory_type(fields.get("type")),
            description=str(fields.get("description") or ""),
            scope=_coerce_scope(fields.get("scope")),
            modified_ts=modified_ts,
        )
        return MemoryFile(
            header=header,
            body=body,
            freshness=memory_freshness_text(modified_ts),
        )

    def format_manifest(self) -> str:
        """Render the manifest for the recall side-call: one line per topic
        file with type, filename, modified timestamp and description."""
        headers = sorted(
            self.scan(), key=lambda item: item.modified_ts, reverse=True
        )
        return "\n".join(header.to_manifest_line() for header in headers)

    # ------------------------------------------------------------------
    # Index limits (write-after measurement, v2.1.214+ behavior)
    # ------------------------------------------------------------------

    def check_index_limits(self, index_text: str) -> tuple[bool, str]:
        """Return (ok, reason). Over the 200-line / 25KB limit is an error so
        the model must merge/trim entries; this matches the modern documented
        behavior (the leaked v2.1.88 silently truncated instead, which is a
        known defect we intentionally do not reproduce)."""
        lines = index_text.splitlines()
        if len(lines) > self.max_index_lines:
            return False, (
                f"MEMORY.md has {len(lines)} lines, exceeding the "
                f"{self.max_index_lines}-line limit. Remove or merge stale "
                "entries, then retry."
            )
        if len(index_text.encode("utf-8")) > self.max_index_bytes:
            return False, (
                f"MEMORY.md is {len(index_text.encode('utf-8'))} bytes, "
                f"exceeding the {self.max_index_bytes}-byte limit. Trim the "
                "index and retry."
            )
        return True, ""

    def check_index_line(self, line: str) -> tuple[bool, str]:
        if len(line) > self.max_index_line_chars:
            return False, (
                f"index line is {len(line)} characters, exceeding the "
                f"{self.max_index_line_chars}-character limit. Shorten the "
                "description and retry."
            )
        return True, ""

    # ------------------------------------------------------------------
    # Writing (two-step: topic file, then index line)
    # ------------------------------------------------------------------

    def save_topic_file(
        self,
        name: str,
        description: str,
        type_: str,
        body: str,
        *,
        scope: str = "private",
        filename: str | None = None,
    ) -> Path:
        """Step 1: write the topic file with frontmatter. Returns its path."""
        clean_name = str(name).strip().rstrip(".md") or Path(filename or "memory").stem
        clean_filename = str(filename or f"{_slugify_filename(clean_name)}.md")
        if not clean_filename.lower().endswith(".md"):
            clean_filename += ".md"
        type_ = _coerce_memory_type(type_)
        scope = _coerce_scope(scope)
        clean_description = str(description or "").strip()
        text = (
            format_frontmatter(clean_name, clean_description, type_, scope)
            + str(body).strip()
            + "\n"
        )
        target = (self.memory_dir / clean_filename).resolve()
        if target.parent != self.memory_dir.resolve():
            raise ValueError(f"memory filename escapes the memory directory: {filename}")
        atomic_write(target, text)
        return target

    def upsert_index_line(
        self,
        filename: str,
        description: str,
        *,
        name: str | None = None,
        type_: str | None = None,
        check_limits: bool = True,
    ) -> tuple[bool, str]:
        """Step 2: add or update the one-line entry in MEMORY.md.

        Returns (ok, message). When *check_limits* is True, a full index
        produces an error message for the model instead of silently truncating
        (write-after measurement).
        """
        filename = Path(filename).name
        display_name = str(name or Path(filename).stem)
        type_ = _coerce_memory_type(type_)
        line = f"- [{display_name}]({filename}) — {str(description or '').strip()}"
        ok, reason = self.check_index_line(line)
        if not ok:
            return False, reason

        # The read-modify-write of MEMORY.md runs inside one cross-process lock
        # so concurrent processes/threads cannot lose an update (last-write-wins
        # on a stale snapshot). atomic_write is told the lock is already held
        # (byte-range locks are not reentrant across a second handle).
        lock_path = self.index_path.parent / _LOCK_FILE
        with _CrossProcessLock(lock_path):
            existing = read_text_stable(self.index_path)
            if not existing.strip():
                existing = f"{INDEX_HEADING}\n"
            elif INDEX_HEADING not in existing:
                existing = f"{INDEX_HEADING}\n\n{existing}"

            pattern = re.compile(
                re.escape(f"]({filename}) —"),
                re.IGNORECASE,
            )
            if pattern.search(existing):
                new_index = pattern.sub(
                    f"]({filename}) — {str(description or '').strip()}", existing, count=1
                )
            else:
                if not existing.endswith("\n"):
                    existing += "\n"
                new_index = existing + line + "\n"

            if check_limits:
                ok, reason = self.check_index_limits(new_index)
                if not ok:
                    return False, reason
            atomic_write(self.index_path, new_index, already_locked=True)
        return True, f"index updated: {line.strip()}"

    def remove_memory(self, filename: str) -> tuple[bool, str]:
        """Delete a topic file and its index line (used by /memory, not by the
        model's ordinary flow)."""
        filename = Path(filename).name
        target = (self.memory_dir / filename).resolve()
        if target.parent != self.memory_dir.resolve():
            return False, f"invalid memory filename: {filename}"
        # File removal + index read-modify-write share one lock so a concurrent
        # upsert cannot interleave with a stale snapshot (same rationale as
        # upsert_index_line).
        lock_path = self.index_path.parent / _LOCK_FILE
        with _CrossProcessLock(lock_path):
            deleted_file = False
            if target.is_file():
                try:
                    target.unlink()
                    deleted_file = True
                except OSError as exc:
                    return False, f"failed to delete {filename}: {exc}"

            existing = read_text_stable(self.index_path)
            pattern = re.compile(rf"^.*\]\({re.escape(filename)}\).*$", re.MULTILINE)
            new_index = pattern.sub("", existing)
            new_index = _collapse_blank_lines(new_index)
            atomic_write(self.index_path, new_index, already_locked=True)
        return True, f"removed {filename}" + (" (file)" if deleted_file else " (index only)")

    def stats(self) -> dict[str, Any]:
        headers = self.scan()
        index_text = self.read_index()
        ok, reason = self.check_index_limits(index_text)
        by_type: dict[str, int] = {}
        for header in headers:
            by_type[header.type] = by_type.get(header.type, 0) + 1
        return {
            "directory": str(self.memory_dir),
            "project_slug": sanitize_project_slug(self.cwd),
            "topic_files": len(headers),
            "by_type": by_type,
            "index_lines": len(index_text.splitlines()) if index_text.strip() else 0,
            "index_bytes": len(index_text.encode("utf-8")),
            "index_within_limits": ok,
            "index_issue": reason or "",
            "limits": {
                "max_index_lines": self.max_index_lines,
                "max_index_bytes": self.max_index_bytes,
                "max_index_line_chars": self.max_index_line_chars,
            },
        }

    # ------------------------------------------------------------------
    # Recall side-call (per user turn, once per queryLoop, <=5 files)
    # ------------------------------------------------------------------

    async def select_relevant_memories(
        self,
        query: str,
        client: Any,
        *,
        recent_tools: list[str] | None = None,
        max_files: int | None = None,
    ) -> list[MemoryFile]:
        """Run the side-call: give a cheap LLM the query + manifest and let it
        choose up to ``max_files`` topic files (Claude Code's
        ``selectRelevantMemories``). Failures degrade to an empty selection —
        the injected index still provides navigation."""
        headers = self.scan()
        if not headers:
            return []
        manifest = self.format_manifest()
        limit = max_files or self.max_selected_files
        tools_section = (
            f"\n\nRecently used tools: {', '.join(recent_tools)}" if recent_tools else ""
        )
        user_content = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"
        try:
            text = await _collect_text(
                client,
                [Message(role="user", content=user_content)],
                system_prompt=SELECT_MEMORIES_SYSTEM_PROMPT,
            )
        except Exception:
            return []
        selected = _parse_selected_filenames(text)
        if not selected:
            return []
        memories: list[MemoryFile] = []
        for filename in selected:
            memory = self.read_topic(filename)
            if memory is not None:
                memories.append(memory)
            if len(memories) >= limit:
                break
        return memories


# ---------------------------------------------------------------------------
# Side-call helpers
# ---------------------------------------------------------------------------


async def _collect_text(
    client: Any,
    messages: list[Message],
    *,
    system_prompt: str,
) -> str:
    """Aggregate a streaming chat() call into plain text; raise on error."""
    parts: list[str] = []
    async for event in client.chat(messages, [], system_prompt=system_prompt):
        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeError(f"side-call failed: {event.get('error')}")
        if event_type == "text_delta":
            parts.append(str(event.get("text") or ""))
    return "".join(parts)


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"selected_memories\"[^{}]*\}", re.DOTALL)


def _parse_selected_filenames(text: str) -> list[str]:
    """Extract selected_memories from possibly noisy model output (JSON block
    or a bare array)."""
    candidate = text.strip()
    block = _JSON_BLOCK_RE.search(text)
    if block:
        candidate = block.group(0)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            value = parsed.get("selected_memories")
            if isinstance(value, list):
                return [str(item) for item in value if isinstance(item, str)]
        elif isinstance(parsed, list):
            return [str(item) for item in parsed if isinstance(item, str)]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: filenames mentioned inside brackets like - name.md
    names = re.findall(r"[\w.\-]+\.md", candidate)
    return names


def _slugify_filename(name: str) -> str:
    slug = _SLUG_CHARS_RE.sub("-", str(name).strip().lower())
    slug = _DUPS_DASH_RE.sub("-", slug).strip("-")
    return slug or "memory"


def _collapse_blank_lines(text: str) -> str:
    collapsed = re.sub(r"\n{3,}", "\n\n", text)
    return collapsed.strip("\n") + "\n" if collapsed.strip() else ""


__all__ = [
    "AutoMemoryStore",
    "MemoryFile",
    "MemoryHeader",
    "atomic_write",
    "memory_dir_for",
    "memory_freshness_text",
    "parse_frontmatter",
    "format_frontmatter",
    "sanitize_project_slug",
    "DEFAULT_MAX_INDEX_LINES",
    "DEFAULT_MAX_INDEX_BYTES",
    "DEFAULT_MAX_INDEX_LINE_CHARS",
]
