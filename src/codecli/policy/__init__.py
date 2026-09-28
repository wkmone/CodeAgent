from __future__ import annotations

from typing import Any


class CommandGuard:
    """Validates commands against a blacklist."""

    def __init__(self, blacklist: list[str] | None = None) -> None:
        self.blacklist = blacklist or []

    def validate(self, command: str) -> None:
        """Raise ValueError if command matches a blacklist entry."""
        for pattern in self.blacklist:
            if pattern in command:
                raise ValueError(f"Command blocked by policy: matches '{pattern}'")


__all__ = ["CommandGuard"]
