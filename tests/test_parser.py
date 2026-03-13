"""Tests for typst_pyexec.core.parser."""

from typst_pyexec.core.parser import Parser


def _parse(src: str):
    return Parser().parse(src)


def test_no_cells():
    cells = _parse("= Hello\n\nSome text without python blocks.\n")
    assert cells == []


def test_single_cell():
    src = "= Title\n\n```python\nx = 1\n```\n"
    cells = _parse(src)
    assert len(cells) == 1
    assert cells[0].cell_id == "cell_1"
    assert cells[0].index == 0
    assert "x = 1" in cells[0].source


def test_multiple_cells_order():
    src = "```python\na = 1\n```\n" "Some text.\n" "```python\nb = 2\n```\n"
    cells = _parse(src)
    assert len(cells) == 2
    assert cells[0].cell_id == "cell_1"
    assert cells[1].cell_id == "cell_2"
    assert "a = 1" in cells[0].source
    assert "b = 2" in cells[1].source


def test_cell_start_end_offsets():
    src = "prefix\n```python\nx = 42\n```\nsuffix"
    cells = _parse(src)
    assert len(cells) == 1
    # The extracted region should round-trip
    assert src[cells[0].start : cells[0].end].startswith("```python")


def test_cell_id_option_is_ignored():
    src = "```python\n%| cell_id: my_cell\nx = 1\n```\n"
    cells = _parse(src)
    assert cells[0].cell_id == "cell_1"
    assert "cell_id" not in cells[0].metadata
    assert "cell_id" not in cells[0].options


def test_metadata_extraction():
    src = "```python\n%| caption: My Plot\nimport matplotlib\n```\n"
    cells = _parse(src)
    assert cells[0].metadata.get("caption") == "My Plot"


def test_non_python_fence_ignored():
    src = "```rust\nfn main() {}\n```\n"
    cells = _parse(src)
    assert cells == []


def test_multiline_cell():
    src = "```python\nimport numpy as np\narr = np.arange(5)\nprint(arr)\n```\n"
    cells = _parse(src)
    assert len(cells) == 1
    assert "import numpy" in cells[0].source
    assert "print(arr)" in cells[0].source


def test_block_options_extraction():
    src = (
        "```python\n"
        "%| execute: false\n"
        "%| caption: My Plot\n"
        "%| img-width: 80%\n"
        "x = 1\n"
        "print(x)\n"
        "```\n"
    )
    cells = _parse(src)
    assert len(cells) == 1
    c = cells[0]
    assert c.options["execute"] == "false"
    assert c.options["caption"] == "My Plot"
    assert c.options["img-width"] == "80%"
    assert c.metadata["caption"] == "My Plot"
    assert "x = 1" in c.source
    assert "%| execute" not in c.source


def test_block_options_must_be_at_top():
    src = "```python\n" "x = 1\n" "%| execute: false\n" "print(x)\n" "```\n"
    cells = _parse(src)
    c = cells[0]
    assert "execute" not in c.options
    assert "%| execute: false" in c.source
