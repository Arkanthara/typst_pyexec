"""Optimized figure capture and export routines for matplotlib.

This module is injected into the kernel once and provides efficient
reusable functions for figure tracking, saving, and metadata extraction.
"""

import json
import os
import uuid

import matplotlib.pyplot as plt
import matplotlib.transforms as mpl_transforms

__all__ = [
    "setup_figure_tracking",
    "CellFigureContext",
    "save_figures_and_metadata",
]

# Padding constants (in inches) around subplot figures
_PAD_LEFT_IN = 0.55
_PAD_RIGHT_IN = 0.15
_PAD_BOTTOM_IN = 0.45
_PAD_TOP_IN = 0.20

_FIGURE_SENTINEL = "__typst_pyexec_FIGURE__:"
_FIGMETA_SENTINEL = "__typst_pyexec_FIGMETA__:"


class CellFigureContext:
    """Encapsulates figure tracking context for a single cell execution.

    This class manages the cell ID, figures directory, and figure tracking
    state, providing a clean interface for figure saving and metadata management.

    Attributes
    ----------
    cell_id : str
        Unique identifier for this cell
    figures_dir : str
        Directory path where figures are saved
    keep_subplots : bool
        If False, saves each subplot as a separate image
    figs_before : set
        Set of matplotlib figure numbers that existed before cell execution
    """

    def __init__(
        self, cell_id: str, figures_dir: str, keep_subplots: bool = False
    ) -> None:
        """Initialize cell figure context.

        Parameters
        ----------
        cell_id : str
            Identifier for this cell
        figures_dir : str or Path
            Directory to save figures into
        keep_subplots : bool
            If False, save each subplot as a separate image
        """
        self.cell_id = cell_id
        self.figures_dir = str(figures_dir)
        self.keep_subplots = keep_subplots
        self.figs_before = set(plt.get_fignums())

    def generate_stem(self, fig_num: int, ax_i: int | None = None) -> str:
        """Generate a filename stem for a figure.

        Parameters
        ----------
        fig_num : int
            Matplotlib figure number
        ax_i : int, optional
            Subplot index (if saving subplots separately)

        Returns
        -------
        str
            Filename stem without extension (e.g., "cell123_1_2" for subplot)
        """
        if ax_i is not None:
            return f"{self.cell_id}_{fig_num}_{ax_i}"
        return f"{self.cell_id}_{fig_num}"

    def new_figure_numbers(self) -> list[int]:
        """Get list of figure numbers created after context initialization.

        Returns
        -------
        list[int]
            Figure numbers of newly created figures
        """
        return [n for n in plt.get_fignums() if n not in self.figs_before]


def setup_figure_tracking() -> None:
    """Disable interactive mode for headless figure rendering."""
    plt.ioff()


def _get_axis_title(ax):
    """Extract the most relevant title from an axis."""
    return (
        (ax.get_title(loc="center") or "")
        or (ax.get_title(loc="left") or "")
        or (ax.get_title(loc="right") or "")
    )


def _clear_axis_title(ax):
    """Clear all title locations from an axis."""
    ax.set_title("")
    ax.set_title("", loc="left")
    ax.set_title("", loc="right")


def _save_transparent(
    fig,
    stem: str,
    figures_dir: str,
    bbox,
    png_dpi: int = 150,
) -> str:
    """Save figure to SVG with PNG fallback.

    Parameters
    ----------
    fig : matplotlib.figure.Figure
    stem : str
        Base filename (without extension)
    figures_dir : str or Path
        Directory to save figures into
    bbox : str or matplotlib.transforms.Bbox
        Bounding box for saved figure
    png_dpi : int
        DPI for PNG fallback

    Returns
    -------
    str
        Path to saved file
    """
    figures_dir = str(figures_dir)
    path_svg = os.path.join(figures_dir, f"{stem}.svg")
    path_out = path_svg
    tmp_svg = f"{path_svg}.tmp-{uuid.uuid4().hex}"
    try:
        fig.savefig(tmp_svg, format="svg", bbox_inches=bbox, transparent=True)
        os.replace(tmp_svg, path_svg)
    except Exception:
        if os.path.exists(tmp_svg):
            try:
                os.remove(tmp_svg)
            except OSError:
                pass
        path_out = os.path.join(figures_dir, f"{stem}.png")
        tmp_png = f"{path_out}.tmp-{uuid.uuid4().hex}"
        fig.savefig(
            tmp_png,
            format="png",
            bbox_inches=bbox,
            dpi=png_dpi,
            transparent=True,
        )
        os.replace(tmp_png, path_out)
    return path_out


def save_figures_and_metadata(context: CellFigureContext) -> None:
    """Save all new figures and extract metadata.

    Processes newly created matplotlib figures, handles subplot extraction,
    and prints figure paths with metadata for capture by the executor.

    Parameters
    ----------
    context : CellFigureContext
        Figure tracking context with cell_id, figures_dir, and settings
    """
    for fig_num in context.new_figure_numbers():
        fig = plt.figure(fig_num)
        suptitle = _get_suptitle(fig)

        if _should_split_subplots(fig, context.keep_subplots):
            try:
                _save_subplots(fig, fig_num, suptitle, context)
            except Exception:
                _save_full_figure(fig, fig_num, suptitle, context)
        else:
            _save_full_figure(fig, fig_num, suptitle, context)

        plt.close(fig)


def _should_split_subplots(fig, keep_subplots: bool) -> bool:
    return (not keep_subplots) and len(fig.axes) > 1


def _get_suptitle(fig) -> str:
    if fig._suptitle is None:
        return ""
    return fig._suptitle.get_text() or ""


def _clear_suptitle(fig) -> None:
    if fig._suptitle is not None:
        fig._suptitle.set_text("")


def _emit_figure(path: str, meta: dict) -> None:
    print(f"{_FIGURE_SENTINEL}{path}")
    print(f"{_FIGMETA_SENTINEL}{json.dumps(meta)}")


def _figure_meta(
    path: str,
    fig_num: int,
    subplot: int,
    is_subplot: bool,
    title: str,
    suptitle: str,
    row: int,
    col: int,
    rows: int,
    cols: int,
) -> dict:
    return {
        "path": path,
        "figure": fig_num,
        "subplot": subplot,
        "is_subplot": is_subplot,
        "title": title,
        "suptitle": suptitle,
        "row": row,
        "col": col,
        "rows": rows,
        "cols": cols,
    }


def _save_full_figure(
    fig, fig_num: int, suptitle: str, context: CellFigureContext
) -> None:
    stem = context.generate_stem(fig_num)
    fig_title = _get_axis_title(fig.axes[0]) if fig.axes else ""

    if fig_title:
        _clear_axis_title(fig.axes[0])
    _clear_suptitle(fig)
    fig.canvas.draw()

    path = _save_transparent(fig, stem, context.figures_dir, "tight", png_dpi=150)
    meta = _figure_meta(
        path,
        fig_num,
        subplot=1,
        is_subplot=False,
        title=fig_title,
        suptitle=suptitle,
        row=0,
        col=0,
        rows=1,
        cols=1,
    )
    _emit_figure(path, meta)


def _save_subplots(
    fig, fig_num: int, suptitle: str, context: CellFigureContext
) -> None:
    _clear_suptitle(fig)

    for ax_i, ax in enumerate(fig.axes, start=1):
        stem = context.generate_stem(fig_num, ax_i)
        title = _get_axis_title(ax)

        layout_state = _capture_layout_state(fig)
        for other_ax in fig.axes:
            other_ax.set_visible(other_ax is ax)

        _clear_axis_title(ax)
        fig.canvas.draw()

        bbox = _subplot_bbox(ax, *fig.get_size_inches())
        path = _save_transparent(fig, stem, context.figures_dir, bbox, png_dpi=200)

        _restore_layout_state(layout_state)

        row, col, rows, cols = _subplot_grid_position(ax, ax_i, len(fig.axes))
        meta = _figure_meta(
            path,
            fig_num,
            subplot=ax_i,
            is_subplot=True,
            title=title,
            suptitle=suptitle,
            row=row,
            col=col,
            rows=rows,
            cols=cols,
        )
        _emit_figure(path, meta)


def _capture_layout_state(fig) -> list[tuple]:
    layout_state: list[tuple] = []
    for ax in fig.axes:
        layout_state.append((ax, ax.get_visible(), ax.get_position().frozen()))
    return layout_state


def _restore_layout_state(layout_state: list[tuple]) -> None:
    for ax, visible, position in layout_state:
        ax.set_visible(visible)
        ax.set_position(position)


def _subplot_bbox(ax, fig_w: float, fig_h: float) -> mpl_transforms.Bbox:
    pos = ax.get_position().frozen()
    x0 = max(0.0, pos.x0 - (_PAD_LEFT_IN / fig_w))
    x1 = min(1.0, pos.x1 + (_PAD_RIGHT_IN / fig_w))
    y0 = max(0.0, pos.y0 - (_PAD_BOTTOM_IN / fig_h))
    y1 = min(1.0, pos.y1 + (_PAD_TOP_IN / fig_h))
    return mpl_transforms.Bbox.from_extents(
        x0 * fig_w,
        y0 * fig_h,
        x1 * fig_w,
        y1 * fig_h,
    )


def _subplot_grid_position(ax, ax_i: int, n_axes: int) -> tuple[int, int, int, int]:
    spec = ax.get_subplotspec()
    rows, cols = (1, n_axes)
    row, col = (ax_i - 1, ax_i - 1)
    if spec is not None:
        gs = spec.get_gridspec()
        rows, cols = gs.nrows, gs.ncols
        row = spec.rowspan.start
        col = spec.colspan.start
    return row, col, rows, cols
