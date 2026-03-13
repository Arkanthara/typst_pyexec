"""Cell execution coordinator with parallel scheduling and cache support."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from typst_pyexec.core.cache import CacheStore
from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.kernel import KernelManager
from typst_pyexec.core.parser import Cell
from typst_pyexec.core.scheduler import ExecutionGroup
from typst_pyexec.utils.hashing import sha256_text

logger = logging.getLogger(__name__)

_FIGURE_SENTINEL_PREFIX = "__typst_pyexec_FIGURE__:"
_FIGMETA_SENTINEL_PREFIX = "__typst_pyexec_FIGMETA__:"
_CACHE_SCHEMA_VERSION = 2


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

    def run(
        self,
        cells: list[Cell],
        groups: list[ExecutionGroup],
        cells_to_run: set[str],
        dag: DependencyGraph | None = None,
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

        # Fast path: all outputs are cache hits, no kernel startup needed.
        if not effective_to_run and self._cache is not None:
            cell_map: dict[str, Cell] = {c.cell_id: c for c in cells}
            cached_results: dict[str, CellResult] = {}
            for cid, cell in cell_map.items():
                if not _option_bool(cell, "execute", True):
                    cached_results[cid] = CellResult(cell_id=cid, from_cache=True)
                    continue
                entry = self._cache.load_by_hash(sha256_text(cell.source))
                if entry is None:
                    effective_to_run.add(cid)
                    continue
                if _is_stale_cache_entry(entry):
                    effective_to_run.add(cid)
                    continue
                cached_results[cid] = CellResult.from_dict(entry, cell_id=cid)
            if not effective_to_run:
                return cached_results

        self._kernel.ensure_running()

        # On a cold kernel, unchanged prerequisite cells must run once to
        # rebuild namespace before target cells can execute.
        if (
            effective_to_run
            and not self._kernel.has_namespace_state
            and dag is not None
        ):
            effective_to_run = dag.required_with_predecessors(effective_to_run)
            logger.info(
                "Cold kernel detected; hydrating namespace with %d prerequisite cell(s).",
                len(effective_to_run),
            )

        cell_map = {c.cell_id: c for c in cells}
        results: dict[str, CellResult] = {}

        for group in groups:
            # Separate cells that can be served from cache
            to_execute = [
                cid
                for cid in group
                if cid in effective_to_run
                and _option_bool(cell_map[cid], "execute", True)
            ]
            cached_ids = [cid for cid in group if cid not in effective_to_run]

            # Load cached results
            for cid in cached_ids:
                cell = cell_map[cid]
                if not _option_bool(cell, "execute", True):
                    results[cid] = CellResult(cell_id=cid, from_cache=True)
                    continue
                h = sha256_text(cell.source)
                if self._cache:
                    entry = self._cache.load_by_hash(h)
                    if entry:
                        if _is_stale_cache_entry(entry):
                            logger.info("Refreshing stale cache for cell %s.", cid)
                            to_execute.append(cid)
                            continue
                        results[cid] = CellResult.from_dict(entry, cell_id=cid)
                        logger.debug("Cell %s served from cache.", cid)
                        continue
                # Fallback: mark as needing execution
                to_execute.append(cid)

            # Execute cells that need running.
            # Because Jupyter kernels are not thread-safe we run sequentially.
            # Independent groups are still useful for future multi-kernel support.
            for cid in to_execute:
                cell = cell_map[cid]
                result = self._execute_one(cell)
                results[cid] = result
                if self._cache:
                    self._cache.save(cid, cell.source, result.to_dict())

        return results

    # ------------------------------------------------------------------
    # Single-cell execution
    # ------------------------------------------------------------------

    def _execute_one(self, cell: Cell) -> CellResult:
        """Execute *cell* in the kernel and return a :class:`CellResult`."""
        logger.info("Executing cell %s…", cell.cell_id)

        # Inject figure-capture preamble before user code
        preamble = _figure_preamble(
            cell.cell_id,
            str(self._figures_dir),
            str(self._working_dir),
            keep_subplots=_option_bool(cell, "keep-subplots", False),
        )
        main_code = preamble + "\n" + cell.source
        raw_main = self._execute_with_recovery(main_code, cell.cell_id)
        # Execute postamble separately so the user's final expression still
        # produces execute_result / display_data in the main execution.
        raw_post = self._execute_with_recovery(_figure_postamble(), cell.cell_id)
        raw = _merge_kernel_results(raw_main, raw_post)

        if raw.get("status") == "error":
            logger.error(
                "Cell %s raised an error:\n%s", cell.cell_id, raw.get("error", "")
            )

        # Extract figure paths from stdout sentinel
        figures = _extract_figures(raw.get("stdout", ""))
        clean_stdout = _strip_figure_sentinels(raw.get("stdout", ""))

        # Extract inline images from display_data
        inline_figures = _extract_inline_images(
            raw.get("display_data", []),
            cell.cell_id,
            self._figures_dir,
        )
        figures.extend(inline_figures)

        return CellResult(
            cell_id=cell.cell_id,
            stdout=clean_stdout,
            stderr=raw.get("stderr", ""),
            display_data=raw.get("display_data", []),
            figures=figures,
            figure_metadata=_extract_figure_metadata(raw.get("stdout", "")),
            error=raw.get("error"),
            status=raw.get("status", "ok"),
            from_cache=False,
        )

    def _execute_with_recovery(self, code: str, cell_id: str) -> dict:
        """Execute kernel code, restarting once on transport-level failure."""
        try:
            return self._kernel.execute(code)
        except Exception as exc:
            logger.error(
                "Kernel error on cell %s: %s — attempting recovery.", cell_id, exc
            )
            self._kernel.restart()
            return self._kernel.execute(code)


# ---------------------------------------------------------------------------
# Figure-capture helpers injected around user code
# ---------------------------------------------------------------------------


def _figure_preamble(
    cell_id: str,
    figures_dir: str,
    working_dir: str,
    keep_subplots: bool,
) -> str:
    """Return Python code that will save matplotlib figures after this cell."""
    safe_dir = figures_dir.replace("\\", "\\\\")
    safe_working_dir = working_dir.replace("\\", "\\\\")
    safe_id = cell_id.replace('"', '\\"')
    return f"""\
import os as __os_typst_pyexec
import matplotlib as __mpl_typst_pyexec
__mpl_typst_pyexec.use("Agg")
import matplotlib.pyplot as __plt_typst_pyexec
__os_typst_pyexec.chdir("{safe_working_dir}")
__typst_pyexec_figs_before = set(__plt_typst_pyexec.get_fignums())
__typst_pyexec_cell_id = "{safe_id}"
__typst_pyexec_figures_dir = "{safe_dir}"
__typst_pyexec_keep_subplots = {"True" if keep_subplots else "False"}
"""


def _figure_postamble() -> str:
    return r"""
import os as __os_typst_pyexec
import json as __json_typst_pyexec
__typst_pyexec_new_figs = [n for n in __plt_typst_pyexec.get_fignums()
                      if n not in __typst_pyexec_figs_before]

__typst_pyexec_pad_left_in = 0.55
__typst_pyexec_pad_right_in = 0.15
__typst_pyexec_pad_bottom_in = 0.45
__typst_pyexec_pad_top_in = 0.20


def __typst_pyexec_axis_title(__typst_pyexec_ax):
    return (
        (__typst_pyexec_ax.get_title(loc="center") or "")
        or (__typst_pyexec_ax.get_title(loc="left") or "")
        or (__typst_pyexec_ax.get_title(loc="right") or "")
    )


def __typst_pyexec_clear_axis_title(__typst_pyexec_ax):
    __typst_pyexec_ax.set_title("")
    __typst_pyexec_ax.set_title("", loc="left")
    __typst_pyexec_ax.set_title("", loc="right")


def __typst_pyexec_save_transparent(__typst_pyexec_fig, __typst_pyexec_stem, __typst_pyexec_bbox, __typst_pyexec_png_dpi):
    __typst_pyexec_path_svg = __os_typst_pyexec.path.join(
        __typst_pyexec_figures_dir, f"{__typst_pyexec_stem}.svg"
    )
    __typst_pyexec_path_out = __typst_pyexec_path_svg
    try:
        __typst_pyexec_fig.savefig(
            __typst_pyexec_path_svg,
            format="svg",
            bbox_inches=__typst_pyexec_bbox,
            transparent=True,
        )
    except Exception:
        __typst_pyexec_path_out = __os_typst_pyexec.path.join(
            __typst_pyexec_figures_dir, f"{__typst_pyexec_stem}.png"
        )
        __typst_pyexec_fig.savefig(
            __typst_pyexec_path_out,
            format="png",
            bbox_inches=__typst_pyexec_bbox,
            dpi=__typst_pyexec_png_dpi,
            transparent=True,
        )
    return __typst_pyexec_path_out


for __typst_pyexec_fn in __typst_pyexec_new_figs:
    __typst_pyexec_fig = __plt_typst_pyexec.figure(__typst_pyexec_fn)
    __typst_pyexec_suptitle = ""
    if __typst_pyexec_fig._suptitle is not None:
        __typst_pyexec_suptitle = __typst_pyexec_fig._suptitle.get_text() or ""
    if (not __typst_pyexec_keep_subplots) and len(__typst_pyexec_fig.axes) > 1:
        try:
            __typst_pyexec_suptitle_obj = __typst_pyexec_fig._suptitle
            if __typst_pyexec_suptitle_obj is not None:
                __typst_pyexec_suptitle_obj.set_text("")
            for __typst_pyexec_ax_i, __typst_pyexec_ax in enumerate(__typst_pyexec_fig.axes, start=1):
                __typst_pyexec_stem = f"{__typst_pyexec_cell_id}_{__typst_pyexec_fn}_{__typst_pyexec_ax_i}"
                __typst_pyexec_title = __typst_pyexec_axis_title(__typst_pyexec_ax)
                __typst_pyexec_layout_state = []
                for __typst_pyexec_other_ax in __typst_pyexec_fig.axes:
                    __typst_pyexec_layout_state.append((
                        __typst_pyexec_other_ax,
                        __typst_pyexec_other_ax.get_visible(),
                        __typst_pyexec_other_ax.get_position().frozen(),
                    ))
                    if __typst_pyexec_other_ax is __typst_pyexec_ax:
                        __typst_pyexec_other_ax.set_visible(True)
                    else:
                        __typst_pyexec_other_ax.set_visible(False)
                __typst_pyexec_clear_axis_title(__typst_pyexec_ax)
                __typst_pyexec_fig.canvas.draw()
                __typst_pyexec_pos = __typst_pyexec_ax.get_position().frozen()
                __typst_pyexec_fig_w, __typst_pyexec_fig_h = __typst_pyexec_fig.get_size_inches()
                __typst_pyexec_x0 = max(0.0, __typst_pyexec_pos.x0 - (__typst_pyexec_pad_left_in / __typst_pyexec_fig_w))
                __typst_pyexec_x1 = min(1.0, __typst_pyexec_pos.x1 + (__typst_pyexec_pad_right_in / __typst_pyexec_fig_w))
                __typst_pyexec_y0 = max(0.0, __typst_pyexec_pos.y0 - (__typst_pyexec_pad_bottom_in / __typst_pyexec_fig_h))
                __typst_pyexec_y1 = min(1.0, __typst_pyexec_pos.y1 + (__typst_pyexec_pad_top_in / __typst_pyexec_fig_h))
                __typst_pyexec_bbox = __mpl_typst_pyexec.transforms.Bbox.from_extents(
                    __typst_pyexec_x0 * __typst_pyexec_fig_w,
                    __typst_pyexec_y0 * __typst_pyexec_fig_h,
                    __typst_pyexec_x1 * __typst_pyexec_fig_w,
                    __typst_pyexec_y1 * __typst_pyexec_fig_h,
                )
                __typst_pyexec_path = __typst_pyexec_save_transparent(
                    __typst_pyexec_fig,
                    __typst_pyexec_stem,
                    __typst_pyexec_bbox,
                    __typst_pyexec_png_dpi=200,
                )
                for __typst_pyexec_other_ax, __typst_pyexec_visible, __typst_pyexec_position in __typst_pyexec_layout_state:
                    __typst_pyexec_other_ax.set_visible(__typst_pyexec_visible)
                    __typst_pyexec_other_ax.set_position(__typst_pyexec_position)
                __typst_pyexec_spec = __typst_pyexec_ax.get_subplotspec()
                __typst_pyexec_rows, __typst_pyexec_cols = (1, len(__typst_pyexec_fig.axes))
                __typst_pyexec_row, __typst_pyexec_col = (__typst_pyexec_ax_i - 1, __typst_pyexec_ax_i - 1)
                if __typst_pyexec_spec is not None:
                    __typst_pyexec_gs = __typst_pyexec_spec.get_gridspec()
                    __typst_pyexec_rows, __typst_pyexec_cols = __typst_pyexec_gs.nrows, __typst_pyexec_gs.ncols
                    __typst_pyexec_row = __typst_pyexec_spec.rowspan.start
                    __typst_pyexec_col = __typst_pyexec_spec.colspan.start

                print(f"__typst_pyexec_FIGURE__:{__typst_pyexec_path}")
                print("__typst_pyexec_FIGMETA__:" + __json_typst_pyexec.dumps({
                    "path": __typst_pyexec_path,
                    "figure": __typst_pyexec_fn,
                    "subplot": __typst_pyexec_ax_i,
                    "is_subplot": True,
                    "title": __typst_pyexec_title,
                    "suptitle": __typst_pyexec_suptitle,
                    "row": __typst_pyexec_row,
                    "col": __typst_pyexec_col,
                    "rows": __typst_pyexec_rows,
                    "cols": __typst_pyexec_cols,
                }))
        except Exception:
            __typst_pyexec_stem = f"{__typst_pyexec_cell_id}_{__typst_pyexec_fn}"
            __typst_pyexec_fig_title = __typst_pyexec_fig.axes[0].get_title() if __typst_pyexec_fig.axes else ""
            if __typst_pyexec_fig_title:
                __typst_pyexec_clear_axis_title(__typst_pyexec_fig.axes[0])
                __typst_pyexec_fig.canvas.draw()
            __typst_pyexec_path = __typst_pyexec_save_transparent(
                __typst_pyexec_fig,
                __typst_pyexec_stem,
                __typst_pyexec_bbox="tight",
                __typst_pyexec_png_dpi=150,
            )
            print(f"__typst_pyexec_FIGURE__:{__typst_pyexec_path}")
            print("__typst_pyexec_FIGMETA__:" + __json_typst_pyexec.dumps({
                "path": __typst_pyexec_path,
                "figure": __typst_pyexec_fn,
                "subplot": 1,
                "is_subplot": False,
                "title": __typst_pyexec_fig_title,
                "suptitle": __typst_pyexec_suptitle,
                "row": 0,
                "col": 0,
                "rows": 1,
                "cols": 1,
            }))
    else:
        __typst_pyexec_stem = f"{__typst_pyexec_cell_id}_{__typst_pyexec_fn}"
        __typst_pyexec_fig_title = __typst_pyexec_fig.axes[0].get_title() if __typst_pyexec_fig.axes else ""
        if __typst_pyexec_fig_title:
            __typst_pyexec_clear_axis_title(__typst_pyexec_fig.axes[0])
            __typst_pyexec_fig.canvas.draw()
        __typst_pyexec_path_out = __typst_pyexec_save_transparent(
            __typst_pyexec_fig,
            __typst_pyexec_stem,
            __typst_pyexec_bbox="tight",
            __typst_pyexec_png_dpi=150,
        )
        print(f"__typst_pyexec_FIGURE__:{__typst_pyexec_path_out}")
        print("__typst_pyexec_FIGMETA__:" + __json_typst_pyexec.dumps({
            "path": __typst_pyexec_path_out,
            "figure": __typst_pyexec_fn,
            "subplot": 1,
            "is_subplot": False,
            "title": __typst_pyexec_fig_title,
            "suptitle": __typst_pyexec_suptitle,
            "row": 0,
            "col": 0,
            "rows": 1,
            "cols": 1,
        }))
    __plt_typst_pyexec.close(__typst_pyexec_fig)
"""


def _extract_figures(stdout: str) -> list[str]:
    paths: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(_FIGURE_SENTINEL_PREFIX):
            paths.append(line[len(_FIGURE_SENTINEL_PREFIX) :].strip())
    return paths


def _extract_figure_metadata(stdout: str) -> list[dict]:
    meta: list[dict] = []
    for line in stdout.splitlines():
        if not line.startswith(_FIGMETA_SENTINEL_PREFIX):
            continue
        payload = line[len(_FIGMETA_SENTINEL_PREFIX) :].strip()
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            meta.append(obj)
    return meta


def _strip_figure_sentinels(stdout: str) -> str:
    lines = [
        line
        for line in stdout.splitlines()
        if not line.startswith(_FIGURE_SENTINEL_PREFIX)
        and not line.startswith(_FIGMETA_SENTINEL_PREFIX)
    ]
    return "\n".join(lines)


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


def _option_bool(cell: Cell, key: str, default: bool) -> bool:
    raw = cell.metadata.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    if int(entry.get("schema_version", 0) or 0) < _CACHE_SCHEMA_VERSION:
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
