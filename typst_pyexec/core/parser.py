"""Parse Python fenced code blocks from Typst source files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Matches fenced blocks:  ```python\n<source>\n```
# The block may be at the start of a line (ignoring leading whitespace).
_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)```python[ \t]*\n(?P<source>.*?)```",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class Cell:
    """A single Python code cell extracted from a Typst document.

    Attributes
    ----------
    cell_id:
        Stable string identifier of the form ``"cell_N"`` (1-based).
    index:
        Zero-based position of the cell in document order.
    source:
        Raw Python source code (without the fence markers).
    start:
        Character offset of the opening fence in the source document.
    end:
        Character offset immediately after the closing fence.
    metadata:
        Optional key-value pairs provided via leading ``%| key: value`` lines.
    """

    cell_id: str
    index: int
    source: str
    start: int
    end: int
    metadata: dict[str, str] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=dict)


class Parser:
    """Extract Python cells from a Typst document string."""

    def parse(self, text: str) -> list[Cell]:
        """Return ordered list of :class:`Cell` objects found in *text*.

        Parameters
        ----------
        text:
            Full content of the ``.typ`` file.

        Returns
        -------
        list[Cell]
            Cells in document order (index 0 = first occurrence).
        """
        cells: list[Cell] = []
        for idx, match in enumerate(_FENCE_RE.finditer(text)):
            raw_source = match.group("source")
            options, source = _extract_block_options(raw_source)
            metadata = dict(options)
            # Cell identifiers are always generated from document order.
            cell_id = f"cell_{idx + 1}"
            cells.append(
                Cell(
                    cell_id=cell_id,
                    index=idx,
                    source=source,
                    start=match.start(),
                    end=match.end(),
                    metadata=metadata,
                    options=options,
                )
            )
        return cells


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_block_options(source: str) -> tuple[dict[str, str], str]:
    """Extract leading `%| key: value` lines and return cleaned source."""
    opts: dict[str, str] = {}
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("%|"):
            break
        body = line[2:].strip()
        if ":" in body:
            key, value = body.split(":", 1)
            norm_key = key.strip().lower()
            # cell_id is reserved and not user-configurable.
            if norm_key == "cell_id":
                i += 1
                continue
            opts[norm_key] = value.strip()
        i += 1

    cleaned = "\n".join(lines[i:])
    if source.endswith("\n"):
        cleaned += "\n"
    return opts, cleaned
