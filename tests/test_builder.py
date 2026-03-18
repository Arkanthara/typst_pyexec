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


def test_detect_changed_cells_marks_toggle_back_to_old_hash_as_changed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    v1 = """```python
x = 1
```
"""
    c1 = _parse_cells(builder, v1)
    builder._cache.save(
        c1[0].cell_id,
        c1[0].source,
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
x = 2
```
"""
    c2 = _parse_cells(builder, v2)
    builder._cache.save(
        c2[0].cell_id,
        c2[0].source,
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

    # Switching back to v1 must still be treated as changed for this cell ID,
    # even though v1 hash already exists in cache history.
    c1_again = _parse_cells(builder, v1)
    changed = builder._detect_changed_cells(c1_again)
    assert changed == {"cell_1"}


def test_resolve_preview_command_prefers_tinymist_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    monkeypatch.setattr("typst_pyexec.builder.shutil.which", lambda name: "tm" if name == "tinymist" else None)

    cmd = builder._resolve_preview_command("auto")
    assert cmd == ["tinymist", "preview", str(builder._intermediate)]


def test_resolve_preview_command_falls_back_to_typst_watch_when_tinymist_missing(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path, compiler="typst")

    monkeypatch.setattr("typst_pyexec.builder.shutil.which", lambda _name: None)

    cmd = builder._resolve_preview_command("auto")
    assert cmd == ["typst", "watch", str(builder._intermediate)]


def test_resolve_preview_command_none_disables_preview(tmp_path: Path) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    cmd = builder._resolve_preview_command("none")
    assert cmd is None


def test_dag_affected_cells_include_dependents_for_report_like_case(tmp_path: Path) -> None:
    source = tmp_path / "doc.typ"
    source.write_text(
        """```python
base = 1
```

```python
x = base + 1
```

```python
print(x)
```
""",
        encoding="utf-8",
    )
    builder = Builder(source=source, output_dir=tmp_path)
    cells = builder._parser.parse(source.read_text(encoding="utf-8"))
    builder._dag.build(cells)

    affected = builder._dag.affected({"cell_1"})
    assert affected == {"cell_1", "cell_2", "cell_3"}


def test_start_preview_falls_back_when_tinymist_exits_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path, compiler="typst")

    class _Proc:
        def __init__(self, returncode):
            self._returncode = returncode

        def poll(self):
            return self._returncode

    calls: list[list[str]] = []

    def _fake_popen(cmd, cwd=None, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "tinymist":
            return _Proc(returncode=1)
        return _Proc(returncode=None)

    monkeypatch.setattr("typst_pyexec.builder.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("typst_pyexec.builder.time.sleep", lambda _x: None)
    monkeypatch.setattr("typst_pyexec.builder.shutil.which", lambda name: "tm" if name == "tinymist" else None)

    proc = builder._start_preview("auto")
    assert proc is not None
    assert calls[0][0] == "tinymist"
    assert calls[1][0] == "typst"
    assert calls[1][1] == "watch"


def test_start_preview_returns_none_when_preview_disabled(tmp_path: Path) -> None:
    source = tmp_path / "doc.typ"
    source.write_text("= t\n", encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path)

    proc = builder._start_preview("none")
    assert proc is None
