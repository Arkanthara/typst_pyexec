"""Tests for typst_pyexec.builder change detection behavior."""

from pathlib import Path

from typst_pyexec.builder import Builder


def _parse_cells(builder: Builder, text: str):
    return builder._parser.parse(text)


def test_detect_changed_cells_ignores_option_only_changes(tmp_path: Path) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    v1 = """```python
%| label: a
print(1)
```
"""
    c1 = _parse_cells(builder, v1)
    assert len(c1) == 1
    builder._cache.save(
        c1[0].cell_id,
        c1[0].source,
        {
            "stdout": "1\n",
            "stderr": "",
            "display_data": [],
            "figures": [],
            "figure_metadata": [],
            "error": None,
            "status": "ok",
        },
    )

    v2 = """```python
%| label: b
print(1)
```
"""
    c2 = _parse_cells(builder, v2)
    changed = builder._detect_changed_cells(c2)
    assert changed == set()


def test_detect_changed_cells_stable_when_inserting_cell_above(tmp_path: Path) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    v1 = """```python
a = 1
```

```python
b = 2
```
"""
    old_cells = _parse_cells(builder, v1)
    for c in old_cells:
        builder._cache.save(
            c.cell_id,
            c.source,
            {
                "stdout": "",
                "stderr": "",
                "display_data": [],
                "figures": [],
                "figure_metadata": [],
                "error": None,
                "status": "ok",
            },
        )

    v2 = """```python
x = 0
```

```python
a = 1
```

```python
b = 2
```
"""
    new_cells = _parse_cells(builder, v2)
    changed = builder._detect_changed_cells(new_cells)

    assert len(changed) == 1
    assert next(iter(changed)) == "cell_1"
