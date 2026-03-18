"""Convert cell execution results to Typst markup and inject into source."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import nbformat

from typst_pyexec.core.executor import (
    CellResult,
    _effective_plot_options,
    _figure_postamble,
    _figure_preamble,
)
from typst_pyexec.core.parser import Cell
from typst_pyexec.utils.options import cell_option_bool

logger = logging.getLogger(__name__)

# Subfigure Typst boilerplate injected once when subfigures are present
_SUBFIGURE_PREAMBLE = r"""
#show figure.where(kind: "subfigure"): set figure(supplement: "Figure")

#show figure.where(kind: image): outer => {
  counter(figure.where(kind: "subfigure")).update(0)
  set figure(numbering: (..nums) => {
    let outer-nums = counter(figure.where(kind: image)).at(outer.location())
    std.numbering("1a", ..outer-nums, ..nums)
  })
  show figure.where(kind: "subfigure"): inner => {
    show figure.caption: it => context {
      std.numbering("(a)", it.counter.at(inner.location()).last())
      [ ]
      it.body
    }
    inner
  }
  outer
}
"""

# Regex that matches a fenced python block in Typst source
_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)```python[ \t]*\n(?P<source>.*?)```",
    re.MULTILINE | re.DOTALL,
)


class Renderer:
    """Render :class:`CellResult` objects into Typst markup.

    Parameters
    ----------
    figures_dir:
        Absolute path to the figures directory.
    state_dir:
        Absolute path to the ``.typst_pyexec/`` state directory.
    """

    def __init__(self, figures_dir: Path, state_dir: Path) -> None:
        self._figures_dir = figures_dir
        self._state_dir = state_dir

    # ------------------------------------------------------------------
    # Document injection
    # ------------------------------------------------------------------

    def inject(
        self,
        source: str,
        cells: list[Cell],
        results: dict[str, CellResult],
    ) -> str:
        """Replace Python fences in *source* with rendered Typst output.

        Parameters
        ----------
        source:
            Raw content of the ``.typ`` file.
        cells:
            Ordered cell list from the parser.
        results:
            Map of cell_id → :class:`CellResult`.

        Returns
        -------
        str
            The intermediate ``.typst_pyexec.typ`` content.
        """
        has_subfigures = self._check_subfigures(cells, results)
        preamble = _SUBFIGURE_PREAMBLE if has_subfigures else ""

        # We replace matches in reverse order to preserve offsets
        matches = list(_FENCE_RE.finditer(source))
        if len(matches) != len(cells):
            logger.warning(
                "Match count (%d) != cell count (%d) — document may be malformed.",
                len(matches),
                len(cells),
            )

        pieces = list(source)  # mutable character list
        for cell, match in zip(reversed(cells), reversed(matches), strict=False):
            result = results.get(cell.cell_id)
            if result is None:
                continue
            rendered = self._render_cell_with_indent(
                cell,
                result,
                block_indent=match.group("indent") or "",
            )
            # Replace the matched region
            start, end = match.start(), match.end()
            pieces[start:end] = list(rendered)

        body = "".join(pieces)
        return preamble + body

    # ------------------------------------------------------------------
    # Cell rendering
    # ------------------------------------------------------------------

    def render_cell(self, cell: Cell, result: CellResult) -> str:
        """Return Typst markup for *result*."""
        return self._render_cell_with_indent(cell, result, block_indent="")

    def _render_cell_with_indent(
        self,
        cell: Cell,
        result: CellResult,
        block_indent: str,
    ) -> str:
        """Return Typst markup for *result* preserving block indentation."""
        parts: list[str] = []
        raw_enabled = cell_option_bool(cell, "raw", True)
        figure_enabled = cell_option_bool(cell, "figure", True)

        if cell_option_bool(cell, "echo", True):
            parts.append(_render_source(cell.source))

        if not cell_option_bool(cell, "execute", True):
            rendered = "\n".join(parts) if parts else ""
            return _indent_block(rendered, block_indent)

        if raw_enabled and result.error:
            parts.append(_render_error(result.error))

        if raw_enabled and result.stdout and result.stdout.strip():
            parts.append(_render_stdout(result.stdout))

        # Render figures
        if figure_enabled and result.figures:
            parts.append(
                self._render_figures(result.figures, result.figure_metadata, cell)
            )

        # Render DataFrames / HTML tables from display_data
        for bundle in result.display_data:
            if "text/html" in bundle:
                typst_table = _html_table_to_typst(bundle["text/html"])
                if typst_table:
                    parts.append(typst_table)
            elif raw_enabled and "text/plain" in bundle:
                txt = bundle["text/plain"].strip()
                if txt:
                    parts.append(_render_stdout(txt))

        rendered = "\n".join(parts) if parts else ""
        return _indent_block(rendered, block_indent)

    # ------------------------------------------------------------------
    # Notebook synchronisation
    # ------------------------------------------------------------------

    def sync_notebook(
        self,
        cells: list[Cell],
        results: dict[str, CellResult],
        notebook_path: Path,
        *,
        mode: Literal["normal", "export"] = "normal",
        working_dir: Path | None = None,
    ) -> None:
        """Write or update the persistent ``.ipynb`` notebook file."""
        if notebook_path.exists():
            try:
                nb = nbformat.read(notebook_path.open(encoding="utf-8"), as_version=4)
            except Exception as exc:
                logger.warning(
                    "Could not read notebook %s (%s); creating a new one.",
                    notebook_path,
                    exc,
                )
                nb = nbformat.v4.new_notebook()
        else:
            nb = nbformat.v4.new_notebook()

        nb_cells: list = []
        if working_dir is not None:
            setup_source = (
                _export_notebook_setup_source(working_dir)
                if mode == "export"
                else _notebook_chdir_source(working_dir)
            )
            nb_cells.append(nbformat.v4.new_code_cell(setup_source))

        for cell in cells:
            result = results.get(cell.cell_id)
            cell_source = (
                _export_mode_source(cell, self._figures_dir, working_dir)
                if mode == "export"
                else cell.source
            )
            nc = nbformat.v4.new_code_cell(cell_source)
            nc["metadata"]["typst_pyexec_cell_id"] = cell.cell_id
            nc["metadata"]["typst_pyexec_mode"] = mode
            if result:
                nc.outputs = _result_to_nb_outputs(result)
            nb_cells.append(nc)

        nb.cells = nb_cells
        nbformat.write(nb, notebook_path.open("w", encoding="utf-8"))
        logger.debug("Notebook synchronised: %s", notebook_path)

    def sync_notebooks(
        self,
        cells: list[Cell],
        results: dict[str, CellResult],
        working_dir: Path,
    ) -> None:
        """Write both the normal and export-mode notebook representations."""
        self.sync_notebook(
            cells,
            results,
            self._state_dir / "notebook.ipynb",
            mode="normal",
            working_dir=working_dir,
        )
        self.sync_notebook(
            cells,
            results,
            self._state_dir / "notebook_export.ipynb",
            mode="export",
            working_dir=working_dir,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_figures(
        self,
        fig_paths: list[str],
        figure_metadata: list[dict],
        cell: Cell,
    ) -> str:
        meta = _normalize_figure_metadata(figure_metadata)
        rel_paths = [
            _relative_figure_path(
                _resolve_exported_figure_path(Path(p)),
                self._state_dir.parent,
            )
            for p in fig_paths
        ]
        caption = cell.metadata.get("caption", "")
        if not caption:
            suptitle = _first_non_empty(m.get("suptitle", "") for m in meta)
            title = _first_non_empty(m.get("title", "") for m in meta)
            caption = _latex_to_typst_text(suptitle or title)
        label = cell.metadata.get("label", "")
        img_args = _passthrough_args(cell, "img-")
        fig_args = _passthrough_args(cell, "fig-")
        grid_args = _passthrough_args(cell, "grid-")

        if len(rel_paths) == 1:
            image_call = _image_call(rel_paths[0], img_args)
            figure_named: list[str] = []
            if caption:
                figure_named.append(f"caption: [{_typst_multiline(caption)}]")
            figure_named.extend(fig_args)
            if figure_named or label:
                args = ", ".join([image_call] + figure_named)
                label_markup = f" <{label}>" if label else ""
                return f"#figure({args}){label_markup}\n"
            return f"#{image_call}\n"

        child_items: list[str] = []
        ordered = _order_paths_with_meta(rel_paths, meta)
        for idx, (rel, m) in enumerate(ordered):
            child_label = _child_label(label, idx)
            child_label_markup = f" <{child_label}>" if child_label else ""
            child_caption = _latex_to_typst_text(str(m.get("title", "")).strip())
            child_caption_arg = (
                f", caption: [{_typst_multiline(child_caption)}]"
                if child_caption
                else ""
            )
            child_items.append(
                f'[#figure({_image_call(rel, img_args)}, kind: "subfigure"{child_caption_arg}){child_label_markup}]'
            )

        default_cols = _infer_default_grid_columns(meta, len(rel_paths))
        columns = cell.metadata.get("grid-columns", str(default_cols))
        grid_named = [f"columns: {columns}"] + grid_args
        grid_call = f"grid({', '.join(grid_named + child_items)})"

        outer_named: list[str] = []
        if caption:
            outer_named.append(f"caption: [{_typst_multiline(caption)}]")
        outer_named.append("kind: image")
        outer_named.extend(fig_args)
        outer_args = ", ".join([grid_call] + outer_named)
        label_markup = f" <{label}>" if label else ""
        return f"#figure({outer_args}){label_markup}\n"

    def _check_subfigures(
        self, cells: list[Cell], results: dict[str, CellResult]
    ) -> bool:
        for cell in cells:
            r = results.get(cell.cell_id)
            if r and len(r.figures) > 1:
                return True
        return False


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


def _render_stdout(text: str) -> str:
    return f'#raw("{_escape_raw(text.rstrip())}")\n'


def _render_source(source: str) -> str:
    code = _format_python_source(source)
    fence = _raw_fence_for_text(code)
    return f"{fence}python\n{code}\n{fence}\n"


def _render_error(traceback: str) -> str:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", traceback)
    return f'#raw("{_escape_raw(clean.rstrip())}", lang: "traceback")\n'


def _indent_block(text: str, indent: str) -> str:
    if not text or not indent:
        return text
    return "\n".join(f"{indent}{line}" if line else indent for line in text.split("\n"))


def _escape_raw(text: str) -> str:
    """Escape text for a Typst string literal passed to ``#raw``."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _raw_fence_for_text(text: str) -> str:
    """Return a backtick fence long enough to contain *text* safely."""
    runs = re.findall(r"`+", text)
    longest = max((len(run) for run in runs), default=0)
    return "`" * max(3, longest + 1)


def _format_python_source(source: str) -> str:
    """Return Black-formatted source when available, else original source."""
    code = source.rstrip("\n")
    try:
        import black as _black

        return _black.format_str(code, mode=_black.FileMode()).rstrip("\n")
    except Exception:
        return code


def _passthrough_args(cell: Cell, prefix: str) -> list[str]:
    args: list[str] = []
    for key, value in cell.metadata.items():
        if key.startswith(prefix) and value:
            args.append(f"{key[len(prefix):]}: {value}")
    return args


def _image_call(path: str, img_args: list[str]) -> str:
    if img_args:
        return f'image("{path}", {", ".join(img_args)})'
    return f'image("{path}")'


def _child_label(parent_label: str, idx: int) -> str:
    if not parent_label:
        return ""
    return f"{parent_label}-{_alpha_suffix(idx)}"


def _normalize_figure_metadata(meta: object) -> list[dict]:
    if not isinstance(meta, list):
        return []
    return [m for m in meta if isinstance(m, dict)]


def _infer_default_grid_columns(meta: list[dict], fallback: int) -> int:
    if not meta:
        return fallback
    cols = [int(m.get("cols", 0) or 0) for m in meta]
    cols = [c for c in cols if c > 0]
    return cols[0] if cols else fallback


def _order_paths_with_meta(
    rel_paths: list[str], meta: list[dict]
) -> list[tuple[str, dict]]:
    if not meta:
        return [(p, {}) for p in rel_paths]
    by_name = {Path(str(m.get("path", ""))).name: m for m in meta}
    by_stem: dict[str, dict] = {}
    for m in meta:
        stem = Path(str(m.get("path", ""))).stem
        if not stem or stem in by_stem:
            continue
        by_stem[stem] = m
    out: list[tuple[str, dict]] = []
    for p in rel_paths:
        pp = Path(p)
        m = by_name.get(pp.name) or by_stem.get(pp.stem) or {}
        out.append((p, m))
    out.sort(
        key=lambda item: (
            int(item[1].get("row", 0) or 0),
            int(item[1].get("col", 0) or 0),
            item[0],
        )
    )
    return out


def _first_non_empty(values) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def _latex_to_typst_text(text: str) -> str:
    if not text:
        return ""
    out = text.replace("\\", "")
    return out.replace("{", "(").replace("}", ")")


def _typst_multiline(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line != ""]
    if len(lines) <= 1:
        return text
    return " #linebreak() ".join(lines)


def _alpha_suffix(idx: int) -> str:
    # 0 -> a, 1 -> b, ... 25 -> z, 26 -> aa
    out = ""
    n = idx
    while True:
        n, rem = divmod(n, 26)
        out = chr(ord("a") + rem) + out
        if n == 0:
            break
        n -= 1
    return out


def _relative_figure_path(fig_path: Path, doc_dir: Path) -> str:
    """Return a path string relative to *doc_dir*, using forward slashes."""
    try:
        rel = fig_path.relative_to(doc_dir)
        return rel.as_posix()
    except ValueError:
        return fig_path.as_posix()


def _resolve_exported_figure_path(fig_path: Path) -> Path:
    """Resolve figure path and fall back across SVG/PNG when needed."""
    if fig_path.exists():
        return fig_path

    if fig_path.suffix.lower() == ".svg":
        alt = fig_path.with_suffix(".png")
        if alt.exists():
            return alt
    elif fig_path.suffix.lower() == ".png":
        alt = fig_path.with_suffix(".svg")
        if alt.exists():
            return alt

    return fig_path


def _html_table_to_typst(html: str) -> str:
    """Very lightweight HTML ``<table>`` → Typst ``#table(…)`` converter."""
    try:
        import re as _re

        rows = _re.findall(r"<tr[^>]*>(.*?)</tr>", html, _re.DOTALL | _re.IGNORECASE)
        if not rows:
            return ""
        typst_rows: list[list[str]] = []
        for row in rows:
            cells = _re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", row, _re.DOTALL | _re.IGNORECASE
            )
            cells = [_re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if cells:
                typst_rows.append(cells)

        if not typst_rows:
            return ""

        ncols = max(len(r) for r in typst_rows)
        cells_markup = ", ".join(f"[{c}]" for row in typst_rows for c in row)
        return f"#table(columns: {ncols}, {cells_markup})\n"
    except Exception as exc:
        logger.debug("HTML table conversion failed: %s", exc)
        return ""


def _result_to_nb_outputs(result: CellResult) -> list:
    outputs = []
    if result.stdout:
        outputs.append(
            nbformat.v4.new_output("stream", name="stdout", text=result.stdout)
        )
    if result.error:
        outputs.append(
            nbformat.v4.new_output(
                "error",
                ename="ExecutionError",
                evalue=result.error[:200],
                traceback=[result.error],
            )
        )
    for bundle in result.display_data:
        outputs.append(nbformat.v4.new_output("display_data", data=bundle, metadata={}))
    return outputs


def _notebook_chdir_source(working_dir: Path) -> str:
    safe_dir = str(working_dir).replace("\\", "\\\\")
    return (
        "import os\n"
        f'os.chdir("{safe_dir}")\n'
        f'print("typst_pyexec working directory: {safe_dir}")\n'
    )


def _export_notebook_setup_source(working_dir: Path) -> str:
    """Return setup code for export notebook with all necessary imports."""
    safe_dir = str(working_dir).replace("\\", "\\\\")
    return (
        "import os\n"
        "import matplotlib.pyplot as plt\n"
        "from typst_pyexec.runtime.figure_export import CellFigureContext, save_figures_and_metadata\n"
        f'os.chdir("{safe_dir}")\n'
        f'print("typst_pyexec working directory: {safe_dir}")\n'
    )


def _export_mode_source(cell: Cell, figures_dir: Path, working_dir: Path | None) -> str:
    cwd = working_dir or Path.cwd()
    preamble = _figure_preamble(
        cell.cell_id,
        str(figures_dir),
        str(cwd),
        keep_subplots=cell_option_bool(cell, "keep-subplots", False),
        plot_options=_effective_plot_options(cell),
    ).rstrip("\n")
    postamble = _figure_postamble().rstrip("\n")
    return f"{preamble}\n{cell.source.rstrip()}\n{postamble}\n"
