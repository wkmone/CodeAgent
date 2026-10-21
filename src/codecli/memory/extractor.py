"""extractor.py — Post-session memory extractor (Auto Dream).

Implements Claude Code's ``runExtraction`` pattern, verified against the
leaked v2.1.88 source:

- **Mutual exclusion**: if the main agent already wrote memories during the
  session (``hasMemoryWritesSince``), the extractor skips.
- **Throttling / double gate**: extraction runs only when the last run is
  ``extract_min_interval_hours`` ago AND at least ``extract_min_sessions``
  sessions have passed since (Auto Dream's "24h + 5 sessions" gates). A
  manual ``dream`` run bypasses both.
- **Restricted tools**: the forked agent only gets the generic file tools,
  scoped to the memory directory (cwd == memory_dir), so it cannot grep
  source files, read code, or run git — the "no verification loop" is
  enforced by the toolset itself.
- **Strict 5-turn budget**: reads (parallel) then writes (parallel), never
  interleaved, capped by ``extract_max_turns``.

Also provides ``migrate_legacy`` to import the old SQLite long-term memory
into the Markdown Auto Memory directory (P1 migration).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codecli.config.config import CodeCliConfig
from codecli.memory.auto_memory import (
    AutoMemoryStore,
    sanitize_project_slug,
)
from codecli.memory.legacy import read_legacy_memories
from codecli.prompt.memory_prompts import (
    MEMORY_FRONTMATTER_EXAMPLE,
    TYPES_SECTION_COMBINED,
    WHAT_NOT_TO_SAVE_SECTION,
)

# Tools the extractor may use: only generic file tools scoped to the memory
# directory. Deliberately excludes bash, grep, web, and everything that would
# allow "verifying" claims against the codebase.
EXTRACTOR_TOOL_ALLOWLIST = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "glob_files",
        "list_dir",
        "get_file_info",
    }
)

EXTRACTOR_SYSTEM_PROMPT = f"""\
You are the memory extractor for CodeCLI. After a conversation, you decide what
is worth remembering across sessions and write it into the Markdown memory
directory using the file tools.

You can ONLY access the memory directory — no source files, no git, no shell.
Base your decisions solely on the conversation content provided in the user
message; do not attempt to verify anything against the project.

{TYPES_SECTION_COMBINED}

{WHAT_NOT_TO_SAVE_SECTION}

## Memory file format
{MEMORY_FRONTMATTER_EXAMPLE}

## Writing procedure (two steps)
1. Write each topic file (frontmatter + body). For feedback/project memories
   lead with the rule/fact, then **Why:** and **How to apply:** lines.
2. Add or update its one-line entry in MEMORY.md:
   `- [name](file.md) — short description`.
Keep MEMORY.md lines under 150 characters and the whole index under 200 lines
/ 25KB. MEMORY.md is always loaded into the system prompt of future sessions,
so keep it concise.

## Turn budget
You have a very limited turn budget. write_file requires a prior read of the
same file only when you edit it. The efficient strategy is: turn 1 — issue all
file reads you might need in parallel; turn 2 — issue all writes/edits in
parallel. Do not interleave reads and writes across multiple turns, and do not
waste turns re-reading the same files.

## If nothing is worth saving
If nothing in the conversation is a durable, non-derivable memory, write
nothing and simply say so.
"""


# ---------------------------------------------------------------------------
# Extraction state (Auto Dream double gate)
# ---------------------------------------------------------------------------


class ExtractState:
    """Persistent per-project extraction bookkeeping.

    Stored at ``<root>/state/<slug>.json``. Session count only increases;
    after a successful extraction both counters reset.
    """

    def __init__(self, root: str | Path, cwd: str):
        self.root = Path(root).expanduser()
        self.slug = sanitize_project_slug(cwd)
        self.path = self.root / "state" / f"{self.slug}.json"
        self.last_extraction_at: str | None = None
        self.sessions_since: int = 0

    def load(self) -> "ExtractState":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        self.last_extraction_at = str(data.get("last_extraction_at") or "")
        self.sessions_since = max(int(data.get("sessions_since") or 0), 0)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        from codecli.memory.auto_memory import atomic_write

        atomic_write(
            self.path,
            json.dumps(
                {
                    "last_extraction_at": self.last_extraction_at,
                    "sessions_since": self.sessions_since,
                },
                indent=2,
            )
            + "\n",
        )

    def _locked(self):
        """Cross-process lock guarding the read-modify-write of this state
        file. A sibling ``.<name>.lock`` is used instead of the JSON file
        itself because save() replaces the JSON via os.replace, which would
        invalidate a lock held on the old inode."""
        from codecli.memory.auto_memory import _CrossProcessLock

        return _CrossProcessLock(self.path.parent / f".{self.path.name}.lock")

    def register_session(self) -> None:
        """Call once after every completed session."""
        with self._locked():
            self.load()
            self.sessions_since += 1
            self.save()

    def mark_extracted(self) -> None:
        with self._locked():
            self.load()
            self.last_extraction_at = datetime.now(UTC).isoformat(timespec="seconds")
            self.sessions_since = 0
            self.save()

    def last_extraction_dt(self) -> datetime | None:
        if not self.last_extraction_at:
            return None
        try:
            return datetime.fromisoformat(self.last_extraction_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def should_extract(self, config: CodeCliConfig, *, now: datetime | None = None) -> bool:
        """Auto Dream double gate: interval AND session count (Claude uses
        24h + 5 new sessions; both conditions must hold)."""
        if not config.auto_memory.extractor_enabled:
            return False
        now = now or datetime.now(UTC)
        last = self.last_extraction_dt()
        interval_ok = True
        if last is not None:
            age = now - last
            if age < timedelta(hours=float(config.auto_memory.extract_min_interval_hours)):
                interval_ok = False
        sessions_ok = self.sessions_since >= int(config.auto_memory.extract_min_sessions)
        return interval_ok and sessions_ok


# ---------------------------------------------------------------------------
# Mutual exclusion
# ---------------------------------------------------------------------------


def has_memory_writes(history: list[Any], memory_dir: Path) -> bool:
    """True when the main agent already wrote into the memory directory during
    this session (write_file / edit_file targeting a path under memory_dir) —
    the extractor must then skip, matching ``hasMemoryWritesSince``.

    The main agent runs with cwd == workspace; a relative path resolves against
    the workspace, so it can never target the memory dir successfully (the
    only working ways are absolute or ``~``-expanded paths). We therefore only
    match absolute/expanded candidates here."""
    if not history:
        return False
    memory_dir = memory_dir.resolve()
    write_tools = {"write_file", "edit_file"}
    for msg in history:
        if getattr(msg, "role", None) != "assistant":
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "")
            if name not in write_tools:
                continue
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            path_value = arguments.get("path") if isinstance(arguments, dict) else None
            if not path_value:
                continue
            try:
                candidate = Path(str(path_value)).expanduser().resolve()
            except OSError:
                continue
            try:
                candidate.relative_to(memory_dir)
                return True
            except ValueError:
                continue
    return False


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------


def build_extraction_prompt(
    messages: list[Any],
    manifest: str,
    *,
    max_window: int = 40,
) -> str:
    """Build the user message for the forked extractor agent.

    Only the last ``max_window`` messages are included; the prompt explicitly
    forbids verification beyond those messages (Claude: "no grepping source
    files, no reading code, no git commands").
    """
    recent = list(messages)[-max_window:]
    lines: list[str] = []
    for msg in recent:
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        text = str(content or "").strip()
        if not text:
            continue
        if len(text) > 800:
            text = text[:797] + "..."
        lines.append(f"<{role}>\n{text}\n</{role}>")
    conversation = "\n\n".join(lines) or "(no conversation content)"

    return f"""\
Review the recent conversation below and update persistent memory if there is
anything durable and non-derivable worth keeping.

Current memory index / topic files:
{manifest or "(no existing memories yet)"}

Rules:
- Save only memories that will still be useful in future sessions (see types
  and exclusions in your system prompt).
- For each memory: write the topic file first (frontmatter: name, description,
  type), then add/update its line in MEMORY.md.
- You have a strict 5-turn budget: read what you need in parallel, then write
  in parallel. Do not interleave.
- Use ONLY the conversation content below. Do not grep source files, do not
  read project code, do not run git — those tools are not available to you and
  verifying would waste your budget. Trust what the user said.
- If the memory directory already covers a topic, skip or update it instead of
  duplicating.
- If nothing is worth saving, write nothing.

## Recent conversation
{conversation}
"""


# ---------------------------------------------------------------------------
# Restricted registry
# ---------------------------------------------------------------------------


def build_extractor_registry(source_registry: Any | None = None) -> Any:
    """Return a ToolRegistry containing only the file tools the extractor may
    use. Falls back to the builtin tool list when *source_registry* is None."""
    from codecli.tools.builtins import get_builtin_tools
    from codecli.tools.registry import ToolRegistry

    registry = ToolRegistry()
    candidates: list[Any] = []
    if source_registry is not None:
        candidates = [source_registry.get(name) for name in EXTRACTOR_TOOL_ALLOWLIST]
    if not candidates or not all(candidate is not None for candidate in candidates):
        by_name = {tool.name: tool for tool in get_builtin_tools()}
        candidates = [by_name[name] for name in EXTRACTOR_TOOL_ALLOWLIST if name in by_name]
    for tool in candidates:
        if tool is not None:
            registry.register(tool)
    return registry


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_extraction(
    cwd: str,
    config: CodeCliConfig,
    llm_client: Any,
    history: list[Any],
    *,
    registry: Any | None = None,
    force: bool = False,
) -> tuple[str, str | list[str]]:
    """Run the post-session extractor.

    Returns (status, detail):
    - ("skip", reason): mutual exclusion or double-gate throttle.
    - ("done", [written files]): extraction succeeded (possibly empty).
    - ("error", message): the run failed.
    """
    if not (config.features.memory and config.auto_memory.enabled):
        return ("skip", "auto memory disabled")
    if not config.auto_memory.extractor_enabled and not force:
        return ("skip", "extractor disabled")

    store = AutoMemoryStore(
        config.auto_memory.root,
        cwd,
        max_index_lines=config.auto_memory.max_index_lines,
        max_index_bytes=config.auto_memory.max_index_bytes,
        max_index_line_chars=config.auto_memory.max_index_line_chars,
    )
    memory_dir = store.memory_dir

    # Whole-run mutual exclusion: two CLI/server/worker processes may both
    # reach the Auto Dream gates; the loser waits for the winner and then
    # re-checks the gates inside the lock (state was just marked extracted,
    # so it skips). The lock file is a sibling of the state file — never the
    # memory files themselves, which os.replace() would swap underneath.
    from codecli.memory.auto_memory import _CrossProcessLock

    state_path = ExtractState(config.auto_memory.root, cwd).path
    extract_lock = _CrossProcessLock(
        state_path.parent / f".{state_path.name}.extract.lock",
        timeout=600.0,
    )
    with extract_lock:
        if not force:
            # has_memory_writes scans the whole session (Claude's
            # hasMemoryWritesSince checks since the last extraction); the
            # window applies only to the extraction prompt, not to this gate.
            if has_memory_writes(history, memory_dir):
                return ("skip", "main agent already wrote memories")
            state = ExtractState(config.auto_memory.root, cwd).load()
            if not state.should_extract(config):
                return ("skip", "Auto Dream gates not met")

        restricted = build_extractor_registry(registry)
        from codecli.agent.agent import Agent
        from codecli.tools.base import ToolContext

        agent = Agent(
            llm_client=llm_client,
            tool_registry=restricted,
            config=config,
            cwd=str(memory_dir),
            approval_callback=None,
            max_turns=int(config.auto_memory.extract_max_turns),
            system_prompt=EXTRACTOR_SYSTEM_PROMPT,
        )
        prompt = build_extraction_prompt(
            history,
            store.format_manifest(),
            max_window=int(config.auto_memory.extract_message_window),
        )
        try:
            await agent.run_complete(prompt)
        except Exception as exc:  # noqa: BLE001 - extraction must never crash the session
            return ("error", f"extraction failed: {exc}")

        written = extract_written_paths(agent.history, memory_dir)
        state = ExtractState(config.auto_memory.root, cwd).load()
        state.mark_extracted()
        return ("done", [str(path.name) for path in written])


def extract_written_paths(history: list[Any], memory_dir: Path) -> list[Path]:
    """Collect topic files the extractor wrote (excluding MEMORY.md) — mirrors
    ``extractWrittenPaths`` filtering out the index file."""
    memory_dir = memory_dir.resolve()
    written: list[Path] = []
    for msg in history:
        if getattr(msg, "role", None) != "assistant":
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "")
            if name not in {"write_file", "edit_file"}:
                continue
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            path_value = arguments.get("path") if isinstance(arguments, dict) else None
            if not path_value:
                continue
            try:
                candidate = Path(str(path_value)).expanduser()
                if not candidate.is_absolute():
                    # extractor runs with cwd == memory_dir: relative paths
                    # are relative to the memory directory
                    candidate = memory_dir / candidate
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate.name == "MEMORY.md":
                continue
            try:
                candidate.relative_to(memory_dir)
            except ValueError:
                continue
            if candidate not in written:
                written.append(candidate)
    return written


# ---------------------------------------------------------------------------
# Legacy SQLite migration
# ---------------------------------------------------------------------------


def migrate_legacy(
    db_path: str | Path,
    scope: str,
    store: AutoMemoryStore,
    *,
    max_items: int = 2_000,
) -> dict[str, Any]:
    """Import the old SQLite long-term memory into the Markdown Auto Memory
    directory (read-only one-time migration; the old database is left intact).

    Mapping: fact/preference → user-ish topics, constraint/decision/project →
    project, correction → feedback. Each entry becomes one topic file with a
    ``> migrated from legacy memory #id`` note, plus its MEMORY.md index line.
    """
    rows = read_legacy_memories(db_path, scope=scope, limit=max_items)
    migrated = 0
    skipped = 0
    by_kind: dict[str, int] = {}
    for row in rows:
        type_ = _legacy_kind_to_type(str(row["kind"]))
        name = _topic_name_from_content(str(row["content"]))
        description = " ".join(str(row["content"]).split())[:100]
        body = (
            f"{row['content']}\n\n> migrated from legacy memory #{row['id']} "
            f"(kind={row['kind']}, source={row['source']})"
        )
        try:
            target = store.save_topic_file(name, description, type_, body, scope="private")
            ok, _msg = store.upsert_index_line(target.name, description, type_=type_)
            if not ok:
                skipped += 1
                continue
        except (OSError, ValueError):
            skipped += 1
            continue
        migrated += 1
        by_kind[type_] = by_kind.get(type_, 0) + 1
    return {"migrated": migrated, "skipped": skipped, "by_type": by_kind}


def _legacy_kind_to_type(kind: str) -> str:
    mapping = {
        "correction": "feedback",
        "preference": "user",
        "constraint": "project",
        "decision": "project",
        "fact": "project",
    }
    return mapping.get(str(kind or "").lower(), "project")


def _topic_name_from_content(content: str) -> str:
    compact = " ".join(str(content or "").split())
    return compact[:50] if compact else "memory"


__all__ = [
    "EXTRACTOR_SYSTEM_PROMPT",
    "EXTRACTOR_TOOL_ALLOWLIST",
    "ExtractState",
    "build_extraction_prompt",
    "build_extractor_registry",
    "extract_written_paths",
    "has_memory_writes",
    "migrate_legacy",
    "run_extraction",
]
