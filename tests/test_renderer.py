"""Tests for typst_pyexec.core.renderer subplot rendering helpers."""

from pathlib import Path

import nbformat

from typst_pyexec.core.executor import CellResult
from typst_pyexec.core.parser import Cell
from typst_pyexec.core.renderer import (
    Renderer,
    _child_label,
    _infer_default_grid_columns,
    _latex_to_typst_text,
    _order_paths_with_meta,
    _resolve_exported_figure_path,
)


def _cell(meta: dict[str, str] | None = None) -> Cell:
    return Cell(
        cell_id="c1",
        index=0,
        source="print('x')\n",
        start=0,
        end=0,
        metadata=meta or {},
    )


def test_latex_to_typst_basic() -> None:
    assert _latex_to_typst_text(r"$\\phi_1$") == "$phi_1$"
    assert _latex_to_typst_text(r"\\alpha_{t}") == "alpha_(t)"


def test_default_grid_columns_from_metadata() -> None:
    meta = [{"cols": 2}, {"cols": 2}]
    assert _infer_default_grid_columns(meta, fallback=4) == 2


def test_order_paths_uses_row_col() -> None:
    paths = ["fig_b.svg", "fig_a.svg"]
    meta = [
        {"path": "fig_a.svg", "row": 0, "col": 0},
        {"path": "fig_b.svg", "row": 0, "col": 1},
    ]
    ordered = _order_paths_with_meta(paths, meta)
    assert [p for p, _ in ordered] == ["fig_a.svg", "fig_b.svg"]


def test_order_paths_matches_meta_by_stem_when_extension_differs() -> None:
    paths = ["fig_a.png", "fig_b.png"]
    meta = [
        {"path": "fig_a.svg", "row": 0, "col": 0, "title": "A"},
        {"path": "fig_b.svg", "row": 0, "col": 1, "title": "B"},
    ]
    ordered = _order_paths_with_meta(paths, meta)
    assert [p for p, _ in ordered] == ["fig_a.png", "fig_b.png"]
    assert ordered[0][1].get("title") == "A"


def test_resolve_exported_figure_path_prefers_png_when_svg_missing(
    tmp_path: Path,
) -> None:
    missing_svg = tmp_path / "plot.svg"
    png = tmp_path / "plot.png"
    png.write_bytes(b"png")
    resolved = _resolve_exported_figure_path(missing_svg)
    assert resolved == png


def test_child_label_hyphen_suffix() -> None:
    assert _child_label("test", 0) == "test-a"
    assert _child_label("test", 1) == "test-b"


def test_render_figures_subplots_with_suptitle_and_child_captions(
    tmp_path: Path,
) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"label": "test", "grid-align": "bottom"})
    fig_paths = [
        str(tmp_path / "state" / "img_1.svg"),
        str(tmp_path / "state" / "img_2.svg"),
    ]
    meta = [
        {
            "path": fig_paths[0],
            "title": r"Stationary process\n$\\phi_1 = 0.1$",
            "suptitle": r"Test on values of \n $\\phi_1$",
            "row": 0,
            "col": 0,
            "rows": 1,
            "cols": 2,
        },
        {
            "path": fig_paths[1],
            "title": r"Mean-reverting process\n$\\phi_1 = 0.5$",
            "suptitle": r"Test on values of \n $\\phi_1$",
            "row": 0,
            "col": 1,
            "rows": 1,
            "cols": 2,
        },
    ]

    rendered = renderer._render_figures(fig_paths, meta, c)
    assert "#figure(grid(" in rendered
    assert "columns: 2" in rendered
    assert "align: bottom" in rendered
    assert 'kind: "subfigure"' in rendered
    assert "<test-a>" in rendered
    assert "<test-b>" in rendered
    assert "caption: [Test on values of" in rendered
    assert "$phi_1$" in rendered


def test_render_figures_grid_columns_override(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"grid-columns": "3", "grid-align": "center"})
    fig_paths = [
        str(tmp_path / "state" / "img_1.svg"),
        str(tmp_path / "state" / "img_2.svg"),
    ]

    rendered = renderer._render_figures(fig_paths, [], c)
    assert rendered.count("columns: 3") == 1
    assert "align: center" in rendered


def test_render_figures_promotes_title_to_caption_when_missing(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({})
    fig_paths = [str(tmp_path / "state" / "plot.svg")]
    meta = [{"path": fig_paths[0], "title": "My Figure Title"}]

    rendered = renderer._render_figures(fig_paths, meta, c)
    assert "#figure(" in rendered
    assert "caption: [My Figure Title]" in rendered


def test_render_cell_raw_false_hides_stdout(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"echo": "false", "raw": "false"})
    r = CellResult(cell_id="c1", stdout="hello\n")
    rendered = renderer.render_cell(c, r)
    assert "hello" not in rendered
    assert "#raw(" not in rendered


def test_render_cell_echo_uses_typst_fence(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"execute": "false"})
    rendered = renderer.render_cell(c, CellResult(cell_id="c1"))
    assert rendered.startswith("```python\n")
    assert 'print("x")' in rendered
    assert rendered.rstrip().endswith("```")


def test_render_cell_figure_false_hides_figure(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"echo": "false", "figure": "false"})
    fig = str(tmp_path / "state" / "plot.svg")
    r = CellResult(
        cell_id="c1",
        figures=[fig],
        figure_metadata=[{"path": fig, "title": "Plot"}],
    )
    rendered = renderer.render_cell(c, r)
    assert "#figure(" not in rendered


def test_render_figures_uses_png_when_svg_path_missing(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    c = _cell({"label": "p"})
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "plot.png").write_bytes(b"png")
    fig_paths = [str(state / "plot.svg")]
    meta = [{"path": str(state / "plot.svg"), "title": "From png"}]
    rendered = renderer._render_figures(fig_paths, meta, c)
    assert 'image("state/plot.png")' in rendered


def test_sync_notebooks_writes_normal_and_export_modes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    figures_dir = state_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    renderer = Renderer(figures_dir=figures_dir, state_dir=state_dir)

    cell = Cell(
        cell_id="c1",
        index=0,
        source="print('hello')\n",
        start=0,
        end=0,
        metadata={"keep-subplots": "false"},
    )
    result = CellResult(cell_id="c1", stdout="hello\n")

    renderer.sync_notebooks([cell], {"c1": result}, working_dir=tmp_path)

    normal_nb = nbformat.read(state_dir / "notebook.ipynb", as_version=4)
    export_nb = nbformat.read(state_dir / "notebook_export.ipynb", as_version=4)

    assert len(normal_nb.cells) == 2
    assert len(export_nb.cells) == 2

    # Check normal setup cell
    assert 'os.chdir("' in normal_nb.cells[0].source
    assert "import os" in normal_nb.cells[0].source

    # Check export setup cell has necessary imports
    assert 'os.chdir("' in export_nb.cells[0].source
    assert "import os" in export_nb.cells[0].source
    assert "import matplotlib.pyplot as plt" in export_nb.cells[0].source
    assert "save_figures_and_metadata" in export_nb.cells[0].source
    assert "setup_figure_tracking()" in export_nb.cells[0].source

    assert normal_nb.cells[1].source == "print('hello')\n"
    assert normal_nb.cells[1].metadata.get("typst_pyexec_mode") == "normal"

    assert "__typst_pyexec_ctx" in export_nb.cells[1].source
    assert "print('hello')" in export_nb.cells[1].source
    assert "save_figures_and_metadata(" in export_nb.cells[1].source
    assert export_nb.cells[1].metadata.get("typst_pyexec_mode") == "export"

    assert normal_nb.cells[1].outputs[0]["output_type"] == "stream"
    assert export_nb.cells[1].outputs[0]["output_type"] == "stream"


def test_inject_preserves_block_padding_for_source_and_output(tmp_path: Path) -> None:
    renderer = Renderer(figures_dir=tmp_path / "figures", state_dir=tmp_path / "state")
    source = "Intro\n\n    ```python\n    print('hello')\n    ```\n"
    cell = Cell(
        cell_id="c1",
        index=0,
        source="print('hello')\n",
        start=0,
        end=0,
        metadata={},
    )
    result = CellResult(cell_id="c1", stdout="hello\n")

    injected = renderer.inject(source, [cell], {"c1": result})
    assert "    ```python" in injected
    assert '    print("hello")' in injected
    assert '    #raw("hello")' in injected
