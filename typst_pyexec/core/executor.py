"""Cell execution coordinator with parallel scheduling and cache support."""

from __future__ import annotations

import ast
import base64
import json
import logging
from pathlib import Path

from typst_pyexec.core.cache import CACHE_SCHEMA_VERSION, CacheStore
from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.kernel import KernelManager
from typst_pyexec.core.parser import Cell
from typst_pyexec.core.scheduler import ExecutionGroup
from typst_pyexec.utils.hashing import sha256_text
from typst_pyexec.utils.options import cell_option_bool, parse_float

logger = logging.getLogger(__name__)

_FIGURE_SENTINEL_PREFIX = "__typst_pyexec_FIGURE__:"
_FIGMETA_SENTINEL_PREFIX = "__typst_pyexec_FIGMETA__:"
_DEFAULT_PLOT_OPTIONS: dict[str, object] = {
    "lines.linewidth": 0.8,
    "axes.linewidth": 0.6,
    "grid.linewidth": 0.2,
    "axes.grid": True,
    # Matplotlib scatter default uses `lines.markersize ** 2` as area.
    # 4.2426... gives ~18pt^2 vs default 36pt^2 (about 2x smaller area).
    "lines.markersize": 4,
}


class CellResult:
    """Holds the execution output for a single cell.

    Attributes
    ----------
    cell_id:
        Identifier matching the source cell.
    stdout:
        Captured standard output text.
    stderr:
        Captured standard error text.
    display_data:
        List of MIME bundles produced by ``display()`` / ``IPython.display``.
    figures:
        Paths of saved figure files (SVG / PNG).
    error:
        Error traceback string, or ``None`` on success.
    status:
        ``"ok"`` or ``"error"``.
    from_cache:
        ``True`` when the result was served from disk cache.
    """

    __slots__ = (
        "cell_id",
        "stdout",
        "stderr",
        "display_data",
        "figures",
        "figure_metadata",
        "error",
        "status",
        "from_cache",
    )

    def __init__(
        self,
        cell_id: str,
        stdout: str = "",
        stderr: str = "",
        display_data: list[dict] | None = None,
        figures: list[str] | None = None,
        figure_metadata: list[dict] | None = None,
        error: str | None = None,
        status: str = "ok",
        from_cache: bool = False,
    ) -> None:
        self.cell_id = cell_id
        self.stdout = stdout
        self.stderr = stderr
        self.display_data = display_data or []
        self.figures = figures or []
        self.figure_metadata = figure_metadata or []
        self.error = error
        self.status = status
        self.from_cache = from_cache

    @classmethod
    def from_dict(cls, d: dict, cell_id: str | None = None) -> CellResult:
        return cls(
            cell_id=cell_id or d.get("cell_id", ""),
            stdout=d.get("stdout", ""),
            stderr=d.get("stderr", ""),
            display_data=d.get("display_data", []),
            figures=d.get("figures", []),
            figure_metadata=d.get("figure_metadata", []),
            error=d.get("error"),
            status=d.get("status", "ok"),
            from_cache=True,
        )

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "display_data": self.display_data,
            "figures": self.figures,
            "figure_metadata": self.figure_metadata,
            "error": self.error,
            "status": self.status,
        }


class Executor:
    """Execute cells using a persistent kernel, with caching and parallelism.

    Parameters
    ----------
    kernel:
        The :class:`~typst_pyexec.core.kernel.KernelManager` to use.
    cache:
        Optional :class:`~typst_pyexec.core.cache.CacheStore`.  Pass ``None``
        to disable caching.
    figures_dir:
        Directory in which figure files are stored.
    n_jobs:
        Number of parallel workers used for independent cell groups.
        ``-1`` means "all CPUs".  Note: direct kernel I/O is sequential;
        parallelism here applies to independent *groups* of cells that
        could run on separate kernels.  For simplicity we keep one kernel
        and execute groups sequentially within the kernel but report the
        grouping to the scheduler.
    """

    def __init__(
        self,
        kernel: KernelManager,
        cache: CacheStore | None,
        figures_dir: Path,
        working_dir: Path,
        n_jobs: int = -1,
    ) -> None:
        self._kernel = kernel
        self._cache = cache
        self._figures_dir = figures_dir
        self._working_dir = working_dir
        self._n_jobs = n_jobs
        self._runtime_initialized = False
        # Tracks cells whose code has been executed in the current kernel session.
        # Builder reuses this Executor across watch rebuilds, so this set can
        # suppress unnecessary prerequisite replay in warm sessions.
        # Cache hits alone do not populate Python namespace state.
        self._executed_cells: set[str] = set()

    def run(
        self,
        cells: list[Cell],
        groups: list[ExecutionGroup],
        cells_to_run: set[str],
        dag: DependencyGraph | None = None,
        cell_hashes: dict[str, str] | None = None,
    ) -> dict[str, CellResult]:
        """Execute all cells respecting dependency order.

        Parameters
        ----------
        cells:
            All cells in document order.
        groups:
            Execution groups from :class:`~typst_pyexec.core.scheduler.Scheduler`.
        cells_to_run:
            IDs of cells that must be (re-)executed.

        Returns
        -------
        dict[str, CellResult]
            Mapping of cell_id → result for every cell.
        """
        effective_to_run = set(cells_to_run)
        cell_map = {c.cell_id: c for c in cells}
        execute_enabled = {
            cid: cell_option_bool(cell, "execute", True)
            for cid, cell in cell_map.items()
        }
        hash_by_id = (
            self._build_hash_map(cells, execute_enabled, cell_hashes)
            if self._cache is not None
            else {}
        )
        cache_entries: dict[str, dict | None] = {}

        # Fast path: all outputs are cache hits, no kernel startup needed.
        if not effective_to_run and self._cache is not None:
            cached_results: dict[str, CellResult] = {}
            for cid in cell_map:
                if not execute_enabled[cid]:
                    cached_results[cid] = CellResult(cell_id=cid, from_cache=True)
                    continue
                cached_result = self._cache_result_for_cell(
                    cid,
                    hash_by_id,
                    cache_entries,
                )
                if cached_result is None:
                    effective_to_run.add(cid)
                    continue
                cached_results[cid] = cached_result
            if not effective_to_run:
                return cached_results

        self._kernel.ensure_running()

        # Initialize runtime (matplotlib, imports) once per kernel
        if not self._runtime_initialized:
            self._kernel.initialize_runtime()
            self._runtime_initialized = True

        # On a cold kernel, unchanged prerequisite cells must run once to
        # rebuild namespace before target cells can execute.
        if (
            effective_to_run
            and not self._kernel.has_namespace_state
            and dag is not None
        ):
            effective_to_run = dag.required_with_predecessors(effective_to_run)
            hydrated_ids = sorted(effective_to_run)
            logger.info(
                "Cold kernel detected; hydrating namespace with %d prerequisite cell(s).",
                len(effective_to_run),
            )
            logger.debug(
                "Hydration plan (cold kernel): %s",
                ", ".join(hydrated_ids),
            )

        # Even when the kernel reports namespace state, this process may be
        # attached to a kernel whose namespace does not contain prerequisites
        # for the currently changed cells (e.g., reconnect across processes).
        if effective_to_run and dag is not None:
            required_predecessors: set[str] = set()
            for cid in effective_to_run:
                required_predecessors.update(dag.required_with_predecessors({cid}))
            missing_namespace_ids = {
                cid
                for cid in required_predecessors
                if execute_enabled.get(cid, True) and cid not in self._executed_cells
            }
            if missing_namespace_ids:
                effective_to_run |= missing_namespace_ids
                hydrated_ids = sorted(missing_namespace_ids)
                logger.info(
                    "Hydrating kernel namespace with %d prerequisite cell(s) not yet executed in this session.",
                    len(missing_namespace_ids),
                )
                logger.info(
                    "Hydrating prerequisite cells: %s",
                    ", ".join(hydrated_ids),
                )

        results: dict[str, CellResult] = {}

        for group in groups:
            to_execute: list[str] = []
            for cid in group:
                if not execute_enabled[cid]:
                    results[cid] = CellResult(cell_id=cid, from_cache=True)
                    continue
                if cid in effective_to_run:
                    to_execute.append(cid)
                    continue

                cached_result = self._cache_result_for_cell(
                    cid,
                    hash_by_id,
                    cache_entries,
                )
                if cached_result is not None:
                    results[cid] = cached_result
                    logger.debug("Cell %s served from cache.", cid)
                    continue

                to_execute.append(cid)

            # Execute cells that need running.
            # Because Jupyter kernels are not thread-safe we run sequentially.
            # Independent groups are still useful for future multi-kernel support.
            for cid in to_execute:
                # If the kernel was restarted mid-build, replay prerequisites
                # needed for this cell into the fresh namespace.
                planned_ids = [cid]
                if dag is not None:
                    required_ids = dag.required_with_predecessors({cid})
                    missing_prereqs = sorted(
                        (
                            rid
                            for rid in required_ids
                            if rid != cid
                            and execute_enabled.get(rid, True)
                            and rid not in self._executed_cells
                        ),
                        key=lambda rid: cell_map[rid].index,
                    )
                    # Always execute the target cell when it is scheduled.
                    # Only missing prerequisites are replayed.
                    planned_ids = [*missing_prereqs, cid]

                for run_id in planned_ids:
                    # Avoid re-running a prerequisite already executed earlier
                    # in this pass (or already materialized in results).
                    if run_id in results and run_id in self._executed_cells:
                        continue

                    run_cell = cell_map[run_id]
                    result = self._execute_one(run_cell)
                    results[run_id] = result
                    if result.status == "ok":
                        self._executed_cells.add(run_id)
                    if self._cache:
                        self._cache.save(run_id, run_cell.source, result.to_dict())

                    # If a prerequisite failed, skip dependent execution with
                    # an explicit message for easier debugging.
                    if result.status == "error" and run_id != cid:
                        results[cid] = CellResult(
                            cell_id=cid,
                            error=(
                                f"Skipped because prerequisite cell {run_id} failed.\n"
                                f"{result.error or ''}".rstrip()
                            ),
                            status="error",
                            from_cache=False,
                        )
                        break

        return results

    def _build_hash_map(
        self,
        cells: list[Cell],
        execute_enabled: dict[str, bool],
        provided_hashes: dict[str, str] | None,
    ) -> dict[str, str]:
        if provided_hashes is not None:
            return provided_hashes
        return {
            cell.cell_id: sha256_text(cell.source)
            for cell in cells
            if execute_enabled.get(cell.cell_id, True)
        }

    def _load_cache_entry(
        self,
        cell_id: str,
        hash_by_id: dict[str, str],
        cache_entries: dict[str, dict | None],
    ) -> dict | None:
        if self._cache is None:
            return None
        if cell_id in cache_entries:
            return cache_entries[cell_id]

        source_hash = hash_by_id.get(cell_id)
        if source_hash is None:
            cache_entries[cell_id] = None
            return None

        entry = self._cache.load_by_hash(source_hash)
        cache_entries[cell_id] = entry
        return entry

    def _cache_result_for_cell(
        self,
        cell_id: str,
        hash_by_id: dict[str, str],
        cache_entries: dict[str, dict | None],
    ) -> CellResult | None:
        """Return cached cell result when available and fresh."""
        entry = self._load_cache_entry(cell_id, hash_by_id, cache_entries)
        if entry is None:
            return None
        if _is_stale_cache_entry(entry):
            logger.info("Refreshing stale cache for cell %s.", cell_id)
            return None
        return CellResult.from_dict(entry, cell_id=cell_id)

    # ------------------------------------------------------------------
    # Single-cell execution
    # ------------------------------------------------------------------

    def _execute_one(self, cell: Cell) -> CellResult:
        """Execute *cell* in the kernel and return a :class:`CellResult`."""
        logger.info("Executing cell %s…", cell.cell_id)
        effective_plot_options = _effective_plot_options(cell)
        timeout = _cell_timeout_seconds(cell)
        logger.info(
            "Cell %s effective plot rcParams: %s",
            cell.cell_id,
            json.dumps(effective_plot_options, sort_keys=True),
        )

        # Inject figure-capture preamble before user code
        preamble = _figure_preamble(
            cell.cell_id,
            str(self._figures_dir),
            str(self._working_dir),
            keep_subplots=cell_option_bool(cell, "keep-subplots", False),
            plot_options=effective_plot_options,
        )
        main_code = preamble + "\n" + cell.source
        raw_main = self._execute_with_recovery(main_code, cell.cell_id, timeout)
        # Execute postamble separately so the user's final expression still
        # produces execute_result / display_data in the main execution.
        # If main execution failed, skip postamble to avoid cascading errors.
        if raw_main.get("status") == "error":
            raw = raw_main
        else:
            raw_post = self._execute_with_recovery(
                _figure_postamble(), cell.cell_id, timeout
            )
            raw = _merge_kernel_results(raw_main, raw_post)

        if raw.get("status") == "error":
            logger.error(
                "Cell %s raised an error:\n%s", cell.cell_id, raw.get("error", "")
            )

        clean_stdout, figures, figure_metadata = _parse_stdout_payload(
            raw.get("stdout", "")
        )

        # Extract inline images from display_data
        inline_figures = _extract_inline_images(
            raw.get("display_data", []),
            cell.cell_id,
            self._figures_dir,
        )
        figures.extend(inline_figures)

        figures, figure_metadata = _normalize_figure_results(
            figures,
            figure_metadata,
            self._figures_dir,
        )

        return CellResult(
            cell_id=cell.cell_id,
            stdout=clean_stdout,
            stderr=raw.get("stderr", ""),
            display_data=raw.get("display_data", []),
            figures=figures,
            figure_metadata=figure_metadata,
            error=raw.get("error"),
            status=raw.get("status", "ok"),
            from_cache=False,
        )

    def _execute_with_recovery(
        self, code: str, cell_id: str, timeout: float | None = None
    ) -> dict:
        """Execute kernel code, restarting once on transport-level failure."""
        try:
            if timeout is None:
                return self._kernel.execute(code)
            return self._kernel.execute(code, timeout=timeout)
        except Exception as exc:
            logger.error(
                "Kernel error on cell %s: %s — attempting recovery.", cell_id, exc
            )
            self._kernel.restart()
            self._runtime_initialized = False
            self._executed_cells.clear()
            self._kernel.initialize_runtime()
            self._runtime_initialized = True
            if timeout is None:
                return self._kernel.execute(code)
            return self._kernel.execute(code, timeout=timeout)


# ---------------------------------------------------------------------------
# Figure-capture helpers injected around user code
# ---------------------------------------------------------------------------


def _figure_preamble(
    cell_id: str,
    figures_dir: str,
    working_dir: str,
    keep_subplots: bool,
    keep_colorbar: bool = True,
    plot_options: dict[str, object] | None = None,
) -> str:
    """Return Python code that initializes figure tracking for this cell.

    The preamble is intentionally self-contained because a kernel can be
    restarted between cells in watch mode.
    """
    safe_dir = figures_dir.replace("\\", "\\\\")
    safe_working_dir = working_dir.replace("\\", "\\\\")
    safe_id = cell_id.replace('"', '\\"')
    plot_options_expr = repr(plot_options or {})
    return f"""\
import os
import matplotlib.pyplot as plt
from typst_pyexec.runtime.figure_export import CellFigureContext, save_figures_and_metadata, setup_figure_tracking
setup_figure_tracking()
os.chdir("{safe_working_dir}")
plt.rcParams.update({plot_options_expr})
__typst_pyexec_ctx = CellFigureContext("{safe_id}", "{safe_dir}", keep_subplots={str(keep_subplots)}, keep_colorbar={str(keep_colorbar)})
"""


def _figure_postamble() -> str:
    """Return minimal code to invoke optimized figure saving."""
    return """
save_figures_and_metadata(__typst_pyexec_ctx)
"""


def _extract_figures(stdout: str) -> list[str]:
    _, paths, _ = _parse_stdout_payload(stdout)
    return paths


def _extract_figure_metadata(stdout: str) -> list[dict]:
    _, _, meta = _parse_stdout_payload(stdout)
    return meta


def _strip_figure_sentinels(stdout: str) -> str:
    clean_stdout, _, _ = _parse_stdout_payload(stdout)
    return clean_stdout


def _parse_stdout_payload(stdout: str) -> tuple[str, list[str], list[dict]]:
    clean_lines: list[str] = []
    figures: list[str] = []
    metadata: list[dict] = []

    for line in stdout.splitlines():
        if line.startswith(_FIGURE_SENTINEL_PREFIX):
            figures.append(line[len(_FIGURE_SENTINEL_PREFIX) :].strip())
            continue

        if line.startswith(_FIGMETA_SENTINEL_PREFIX):
            payload = line[len(_FIGMETA_SENTINEL_PREFIX) :].strip()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                metadata.append(obj)
            continue

        clean_lines.append(line)

    return "\n".join(clean_lines), figures, metadata


def _normalize_figure_results(
    figures: list[str],
    figure_metadata: list[dict],
    figures_dir: Path,
) -> tuple[list[str], list[dict]]:
    state_dir = figures_dir.parent
    output_dir = state_dir.parent
    state_dir_name = state_dir.name

    normalized_figures = [
        _normalize_figure_path(p, output_dir, state_dir_name) for p in figures
    ]

    normalized_meta: list[dict] = []
    for meta in figure_metadata:
        if not isinstance(meta, dict):
            continue
        updated = dict(meta)
        path = updated.get("path")
        if isinstance(path, str) and path:
            updated["path"] = _normalize_figure_path(
                path,
                output_dir,
                state_dir_name,
            )
        normalized_meta.append(updated)

    return normalized_figures, normalized_meta


def _normalize_figure_path(path: str, output_dir: Path, state_dir_name: str) -> str:
    raw = str(path).strip()
    if not raw:
        return raw

    normalized = raw.replace("\\", "/")
    marker = f"/{state_dir_name}/figures/"
    idx = normalized.rfind(marker)
    if idx != -1:
        return normalized[idx + 1 :]

    direct = f"{state_dir_name}/figures/"
    if normalized.startswith(direct):
        return normalized
    if normalized.startswith(f"./{direct}"):
        return normalized[2:]

    try:
        candidate = Path(normalized)
        if candidate.is_absolute():
            try:
                return candidate.relative_to(output_dir).as_posix()
            except ValueError:
                pass
    except Exception:
        pass

    return normalized


def _extract_inline_images(
    display_data: list[dict],
    cell_id: str,
    figures_dir: Path,
) -> list[str]:
    """Save any PNG/SVG data found in *display_data* to *figures_dir*."""
    paths: list[str] = []
    for idx, bundle in enumerate(display_data):
        if "image/svg+xml" in bundle:
            fname = f"{cell_id}_display_{idx}.svg"
            path = figures_dir / fname
            path.write_text(bundle["image/svg+xml"], encoding="utf-8")
            paths.append(str(path))
        elif "image/png" in bundle:
            fname = f"{cell_id}_display_{idx}.png"
            path = figures_dir / fname
            path.write_bytes(base64.b64decode(bundle["image/png"]))
            paths.append(str(path))
    return paths


def _plot_options(cell: Cell) -> dict[str, object]:
    """Return matplotlib rcParams overrides from `%| plt-*` metadata."""
    options: dict[str, object] = {}
    for key, value in cell.metadata.items():
        if not key.startswith("plt-"):
            continue
        rc_param = key[4:].strip()
        if not rc_param:
            continue
        options[rc_param] = _parse_plot_option_value(value)
    return options


def _effective_plot_options(cell: Cell) -> dict[str, object]:
    """Return final rcParams for a cell with precedence defaults < `%| plt-*`."""
    return {**_DEFAULT_PLOT_OPTIONS, **_plot_options(cell)}


def _parse_plot_option_value(value: str) -> object:
    """Parse `%| plt-*` option values to Python primitives when possible."""
    raw = value.strip()
    if not raw:
        return ""

    lower = raw.lower()
    if lower in {"none", "null"}:
        return None
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False

    # Prefer JSON for numbers/lists/objects; fall back to Python literals.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _cell_timeout_seconds(cell: Cell) -> float | None:
    """Return per-cell timeout override in seconds, if provided."""
    raw = cell.metadata.get("timeout") or cell.metadata.get("cell-timeout")
    if raw is None:
        return None
    value = parse_float(raw, -1.0)
    if value <= 0:
        return None
    return value


def _merge_kernel_results(primary: dict, secondary: dict) -> dict:
    """Merge two kernel execute payloads while preserving display outputs."""
    stdout = primary.get("stdout", "")
    if secondary.get("stdout"):
        stdout = f"{stdout}{secondary.get('stdout', '')}"

    stderr = primary.get("stderr", "")
    if secondary.get("stderr"):
        stderr = f"{stderr}{secondary.get('stderr', '')}"

    display_data = list(primary.get("display_data", [])) + list(
        secondary.get("display_data", [])
    )

    error = primary.get("error") or secondary.get("error")
    status = (
        "error"
        if (primary.get("status") == "error" or secondary.get("status") == "error")
        else "ok"
    )

    return {
        "stdout": stdout,
        "stderr": stderr,
        "display_data": display_data,
        "error": error,
        "status": status,
    }


def _is_stale_cache_entry(entry: dict) -> bool:
    """Return True when cache entry is from an older schema or shape."""
    if int(entry.get("schema_version", 0) or 0) < CACHE_SCHEMA_VERSION:
        return True

    # Never serve a previously-errored cell from cache: the error may have
    # been transient (e.g. wrong working directory, missing file that now
    # exists).  Re-running costs nothing when the user has fixed the issue.
    if entry.get("status") == "error":
        return True

    # Compatibility guard for figure-aware rendering.
    figures = entry.get("figures", [])
    if not isinstance(figures, list) or not figures:
        return False
    meta = entry.get("figure_metadata")
    if meta is None:
        return True
    return not isinstance(meta, list)
