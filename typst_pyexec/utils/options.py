"""Option-parsing helpers shared across modules."""

from __future__ import annotations

from typst_pyexec.core.parser import Cell

_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def parse_bool(value: str | None, default: bool) -> bool:
    """Parse a user option string to bool with a default fallback."""
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY_VALUES


def cell_option_bool(cell: Cell, key: str, default: bool) -> bool:
    """Read a boolean option from a cell metadata mapping."""
    return parse_bool(cell.metadata.get(key), default)
