"""Tests for atomic figure export writes."""

import json
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from typst_pyexec.runtime.figure_export import (
    CellFigureContext,
    _save_full_figure,
    _save_transparent,
    save_figures_and_metadata,
)


def _extract_emitted_paths(stdout: str) -> list[str]:
    prefix = "__typst_pyexec_FIGURE__:"
    return [
        line[len(prefix) :] for line in stdout.splitlines() if line.startswith(prefix)
    ]


def _extract_emitted_meta(stdout: str) -> list[dict]:
    prefix = "__typst_pyexec_FIGMETA__:"
    out: list[dict] = []
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        out.append(json.loads(line[len(prefix) :]))
    return out


class _DummyFigure:
    def __init__(self, fail_svg: bool = False) -> None:
        self.fail_svg = fail_svg

    def savefig(self, path: str, format: str, **kwargs) -> None:
        if format == "svg" and self.fail_svg:
            raise RuntimeError("svg export failed")

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if format == "svg":
            p.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8"
            )
        else:
            p.write_bytes(b"PNG")


def test_save_transparent_writes_svg_atomically(tmp_path: Path) -> None:
    fig = _DummyFigure(fail_svg=False)

    out = _save_transparent(fig, "cell_1_1", str(tmp_path), bbox="tight")

    assert out.endswith(".svg")
    out_path = Path(out)
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("<svg")
    assert not list(tmp_path.glob("*.tmp-*"))


def test_save_transparent_falls_back_to_png_atomically(tmp_path: Path) -> None:
    fig = _DummyFigure(fail_svg=True)

    out = _save_transparent(fig, "cell_1_2", str(tmp_path), bbox="tight")

    assert out.endswith(".png")
    out_path = Path(out)
    assert out_path.exists()
    assert out_path.read_bytes() == b"PNG"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_save_figures_requires_explicit_show(tmp_path: Path, capsys) -> None:
    plt.close("all")
    ctx = CellFigureContext("cell_no_show", str(tmp_path))

    fig = plt.figure()
    ax = fig.add_subplot(111)
    ax.plot([1, 2, 3], [1, 4, 9])

    save_figures_and_metadata(ctx)

    emitted = capsys.readouterr().out
    assert _extract_emitted_paths(emitted) == []
    assert _extract_emitted_meta(emitted) == []
    assert plt.get_fignums() == []


def test_save_figures_exports_only_shown_figures(tmp_path: Path, capsys) -> None:
    plt.close("all")
    ctx = CellFigureContext("cell_shown_only", str(tmp_path))

    fig_a = plt.figure()
    fig_a.add_subplot(111).plot([0, 1], [0, 1])

    fig_b = plt.figure()
    fig_b.add_subplot(111).plot([0, 1], [1, 0])

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="FigureCanvasAgg is non-interactive, and thus cannot be shown",
            category=UserWarning,
        )
        fig_a.show()
    save_figures_and_metadata(ctx)

    emitted = capsys.readouterr().out
    paths = _extract_emitted_paths(emitted)
    assert len(paths) == 1
    assert f"cell_shown_only_{fig_a.number}" in Path(paths[0]).stem


def test_save_full_figure_keeps_multi_axis_titles(tmp_path: Path, capsys) -> None:
    plt.close("all")
    ctx = CellFigureContext("cell_keep_titles", str(tmp_path), keep_subplots=True)

    fig, axes = plt.subplots(1, 2)
    axes[0].set_title("Left Title")
    axes[1].set_title("Right Title")

    _save_full_figure(fig, fig.number, suptitle="", context=ctx)

    emitted = capsys.readouterr().out
    meta = _extract_emitted_meta(emitted)
    assert meta
    assert meta[0].get("title", "") == ""
    assert axes[0].get_title() == "Left Title"

    plt.close(fig)
