"""Disk-based execution cache keyed by SHA-256 of cell source."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from typst_pyexec.utils.hashing import sha256_text

logger = logging.getLogger(__name__)

_CACHE_SCHEMA_VERSION = 2


class CacheStore:
    """Persistent JSON cache stored under *cache_dir*.

    Each entry is a file named ``<sha256>.json`` and contains the
    execution result for the cell with that source hash.

    Parameters
    ----------
    cache_dir:
        Directory in which cache files are stored.  Created on demand.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def load(self, cell_id: str) -> dict | None:
        """Return cached entry for *cell_id*, or ``None`` if not found.

        The cache entry is matched by *cell_id* via a lookup file that
        maps cell IDs to their most-recent hash.
        """
        lookup = self._lookup_file(cell_id)
        if not lookup.exists():
            return None
        try:
            ref = json.loads(lookup.read_text(encoding="utf-8"))
            h = ref.get("hash")
            entry_file = self._entry_file(h)
            if entry_file.exists():
                return json.loads(entry_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Cache miss for %s: %s", cell_id, exc)
        return None

    def load_by_hash(self, source_hash: str) -> dict | None:
        """Return cached entry by *source_hash*, or ``None``."""
        entry_file = self._entry_file(source_hash)
        if not entry_file.exists():
            return None
        try:
            return json.loads(entry_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def latest_hash(self, cell_id: str) -> str | None:
        """Return most-recent source hash recorded for *cell_id*, if any."""
        lookup = self._lookup_file(cell_id)
        if not lookup.exists():
            return None
        try:
            ref = json.loads(lookup.read_text(encoding="utf-8"))
            h = ref.get("hash")
            return h if isinstance(h, str) and h else None
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, cell_id: str, source: str, result: dict) -> None:
        """Persist *result* for *cell_id* with *source*'s hash as key."""
        h = sha256_text(source)
        entry: dict = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "hash": h,
            "cell_id": cell_id,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "display_data": result.get("display_data", []),
            "figures": result.get("figures", []),
            "figure_metadata": result.get("figure_metadata", []),
            "error": result.get("error"),
            "status": result.get("status", "ok"),
        }
        entry_file = self._entry_file(h)
        entry_file.write_text(json.dumps(entry, indent=2), encoding="utf-8")

        # Update the lookup file
        lookup = self._lookup_file(cell_id)
        lookup.write_text(json.dumps({"hash": h}), encoding="utf-8")

        logger.debug("Cached cell %s (hash=%s…)", cell_id, h[:8])

    def invalidate(self, cell_id: str) -> None:
        """Remove the lookup entry for *cell_id* (does not delete the data file)."""
        lookup = self._lookup_file(cell_id)
        if lookup.exists():
            lookup.unlink()

    def clear(self) -> None:
        """Delete all cache files."""
        for f in self._dir.iterdir():
            f.unlink(missing_ok=True)
        logger.info("Cache cleared.")

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _entry_file(self, source_hash: str) -> Path:
        return self._dir / f"{source_hash}.json"

    def _lookup_file(self, cell_id: str) -> Path:
        safe = cell_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"_id_{safe}.json"
