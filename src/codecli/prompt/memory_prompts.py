"""memory_prompts.py — Prompt constants for the Claude Code style Auto Memory.

The wording below is aligned with the memory-related prompt constants found in
the leaked Claude Code v2.1.88 source (``utils/memory`` and ``src/memdir``),
cross-verified across four independent analyses:

- TYPES_SECTION_COMBINED: four memory types with scope and body structure.
- WHAT_NOT_TO_SAVE_SECTION: explicit exclusions that apply even when the user
  asks to save.
- MEMORY_DRIFT_CAVEAT: memories are point-in-time observations; verify before
  acting and trust current observations on conflict.
- MEMORY_FRONTMATTER_EXAMPLE: the exact frontmatter schema.
- SELECT_MEMORIES_SYSTEM_PROMPT: the side-call selection prompt (<=5 files,
  selective, tool-usage aware).

These are injected verbatim-ish into the system prompt so the model maintains
the memory directory with the generic file tools, exactly like Claude Code.
"""

from __future__ import annotations

TYPES_SECTION_COMBINED = """\
## Types of memory

There are several discrete types of memory that you can store in your memory
system. Each type below declares a <scope> of `private`, `team`, or guidance
for choosing between the two. Team scope is only active when team memory is
enabled; otherwise everything is private.

- **user** (scope: always private)
  Contain information about the user's role, goals, responsibilities, and
  knowledge. Great user memories help you tailor your future behavior to the
  user's preferences and perspective. Save when you learn any details about
  the user's role, preferences, responsibilities, or knowledge.

- **feedback** (scope: default private; save as team only when the guidance is
  clearly a project-wide convention)
  Guidance the user has given you about how to approach work — both what to
  avoid and what to keep doing. Record from failure AND success: if you only
  save corrections, you will avoid past mistakes but drift away from
  approaches the user has already validated. Save any time the user corrects
  your approach OR confirms a non-obvious approach worked.
  Body structure: lead with the rule itself, then a **Why:** line and a
  **How to apply:** line.

- **project** (scope: strongly bias toward team)
  Information about ongoing work, goals, initiatives, bugs, or incidents that
  is not derivable from code or git history. Save when you learn who is doing
  what, why, or by when.
  Body structure: lead with the fact, then **Why:** and **How to apply:**
  lines.

- **reference** (scope: usually team)
  Pointers to where information can be found in external systems. Save when
  you learn about resources in external systems and their purpose.
"""

WHAT_NOT_TO_SAVE_SECTION = """\
## What NOT to save in memory

- Code patterns, conventions, architecture, file paths, or project structure
  — these can be derived by reading the current project state.
- Git history, recent changes, or who-changed-what — `git log` / `git blame`
  are authoritative.
- Debugging solutions or fix recipes — the fix is in the code; the commit
  message has the context.
- Anything already documented in the project instruction files (AGENTS.md
  equivalents).
- Ephemeral task details: in-progress work, temporary state, current
  conversation context.

These exclusions apply even when the user explicitly asks you to save. If they
ask you to save a PR list or activity summary, ask what was *surprising* or
*non-obvious* about it — that is the part worth keeping.
"""

MEMORY_DRIFT_CAVEAT = """\
Memory records can become stale over time. Use memory as context for what was
true at a given point in time. Before answering the user or building
assumptions based solely on information in memory records, verify that the
memory is still correct and up-to-date by reading the current state of the
files or resources. If a recalled memory conflicts with current information,
trust what you observe now — and update or remove the stale memory rather than
acting on it.
"""

MEMORY_FRONTMATTER_EXAMPLE = """\
```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance}}
type: {{user, feedback, project, reference}}
---

{{memory content — for feedback/project: rule/fact, then **Why:** and **How to apply:**}}
```
"""

INDEX_LINE_LIMIT_HINT = """\
`MEMORY.md` is always loaded into your system prompt — lines after 200 will be
truncated, so keep the index concise. Each index line must stay under 150
characters and follow the format:
`- [name](file.md) — short description of what the memory contains`.
"""

SELECT_MEMORIES_SYSTEM_PROMPT = """\
You are selecting memories that will be useful to an agent as it processes a
user's query. You will be given the user's query and a list of available
memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful to the
agent as it processes the user's query (up to 5). Only include memories that
you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful in processing the user's query,
  then do not include it in your list. Be selective and discerning.
- If there are no memories in the list that would clearly be useful, feel free
  to return an empty list.
- If a list of recently-used tools is provided, do not select memories that
  are usage reference or API documentation for those tools (the agent is
  already exercising them). DO still select memories containing warnings,
  gotchas, or known issues about those tools — active use is exactly when
  those matter.

Respond with JSON only, in this exact shape:
{"selected_memories": ["file.md", ...]}
"""


def build_memory_guidelines(memory_dir: str) -> str:
    """Return the system-prompt block that teaches the model to maintain the
    Auto Memory directory with the generic file tools (no dedicated tools)."""
    return f"""\
## Auto memory

You maintain a persistent markdown memory directory for this workspace:

    {memory_dir}

It survives across sessions and is loaded back into your context on future
runs. Manage it yourself with the ordinary file tools (write_file, edit_file,
read_file, glob, list_dir) — there are no dedicated memory tools.

Structure:
- `MEMORY.md` is the index. One line per topic file:
  `- [name](file.md) — short description`. Keep every line under 150
  characters and the whole index under 200 lines / 25KB.
- Each topic file starts with frontmatter:

{MEMORY_FRONTMATTER_EXAMPLE}

{TYPES_SECTION_COMBINED}

{WHAT_NOT_TO_SAVE_SECTION}

## Saving a memory (two steps)
1. Write the topic file with frontmatter (name, description, type).
2. Add or update its one-line entry in MEMORY.md.
For feedback/project memories lead the body with the rule/fact, then
**Why:** and **How to apply:** lines.

## Reading memories
MEMORY.md's first 200 lines are already injected into your system prompt — use
them as navigation. When the injected topic-file content is absent, read a
file only when the index description says it is clearly relevant to the
current request. Recall is selective: prefer the few files that matter over
dumping everything.

## Memory hygiene
- Memories are point-in-time observations, not live state. Verify against the
  current code before acting on a claim from memory.
- When the index is full, first remove or merge stale entries (the user or you
  can edit MEMORY.md like any file); there is no automatic eviction.

{MEMORY_DRIFT_CAVEAT}
"""


__all__ = [
    "TYPES_SECTION_COMBINED",
    "WHAT_NOT_TO_SAVE_SECTION",
    "MEMORY_DRIFT_CAVEAT",
    "MEMORY_FRONTMATTER_EXAMPLE",
    "INDEX_LINE_LIMIT_HINT",
    "SELECT_MEMORIES_SYSTEM_PROMPT",
    "build_memory_guidelines",
]
