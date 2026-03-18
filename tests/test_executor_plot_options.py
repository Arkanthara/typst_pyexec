"""Unit tests for matplotlib `%| plt-*` option handling."""

from __future__ import annotations

from typst_pyexec.core.executor import (
    _effective_plot_options,
    _figure_preamble,
    _parse_plot_option_value,
    _plot_options,
)
from typst_pyexec.core.parser import Cell


def _cell(metadata: dict[str, str]) -> Cell:
    return Cell(cell_id="c1", index=0, source="print('x')\n", start=0, end=1, metadata=metadata)


def test_parse_plot_option_value_primitives() -> None:
    assert _parse_plot_option_value("0.75") == 0.75
    assert _parse_plot_option_value("10") == 10
    assert _parse_plot_option_value("true") is True
    assert _parse_plot_option_value("none") is None
    assert _parse_plot_option_value("'miter'") == "miter"


def test_plot_options_extracts_plt_prefix_only() -> None:
    cell = _cell(
        {
            "plt-lines.linewidth": "0.8",
            "plt-axes.linewidth": "0.6",
            "caption": "ignored",
        }
    )
    assert _plot_options(cell) == {
        "lines.linewidth": 0.8,
        "axes.linewidth": 0.6,
    }


def test_figure_preamble_includes_rcparams_update() -> None:
    preamble = _figure_preamble(
        "c1",
        "C:/tmp/figures",
        "C:/tmp",
        keep_subplots=False,
        plot_options={"lines.linewidth": 0.8, "axes.grid": True},
    )
    assert "plt.rcParams.update(" in preamble
    assert "'lines.linewidth': 0.8" in preamble
    assert "'axes.grid': True" in preamble


def test_effective_plot_options_defaults_are_applied() -> None:
    cell = _cell({})
    assert _effective_plot_options(cell) == {
        "lines.linewidth": 0.8,
        "axes.linewidth": 0.6,
        "grid.linewidth": 0.2,
        "axes.grid": True,
        "lines.markersize": 4,
    }


def test_effective_plot_options_metadata_overrides_defaults() -> None:
    cell = _cell(
        {
            "plt-lines.linewidth": "1.2",
            "plt-axes.grid": "false",
        }
    )
    assert _effective_plot_options(cell) == {
        "lines.linewidth": 1.2,
        "axes.linewidth": 0.6,
        "grid.linewidth": 0.2,
        "axes.grid": False,
        "lines.markersize": 4,
    }
