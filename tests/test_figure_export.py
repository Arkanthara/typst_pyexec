"""Tests for atomic figure export writes."""

from pathlib import Path

from typst_pyexec.runtime.figure_export import _save_transparent


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
