"""Integration tests for cell execution via Jupyter kernel.

These tests start a real Python kernel and verify end-to-end behaviour.
They require ``ipykernel`` to be installed.

Mark them skipped in CI environments that cannot start kernels by
setting the ``typst_pyexec_SKIP_KERNEL_TESTS`` environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from typst_pyexec.builder import Builder
from typst_pyexec.core.cache import CacheStore
from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.executor import Executor
from typst_pyexec.core.kernel import KernelManager
from typst_pyexec.core.parser import Cell
from typst_pyexec.core.scheduler import Scheduler

SKIP = os.environ.get("typst_pyexec_SKIP_KERNEL_TESTS", "").lower() in (
    "1",
    "true",
    "yes",
)


def _make_cell(cell_id: str, source: str, index: int = 0) -> Cell:
    return Cell(cell_id=cell_id, index=index, source=source, start=0, end=len(source))


@pytest.fixture(scope="module")
def kernel_state_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("state")


@pytest.fixture(scope="module")
def kernel(kernel_state_dir):
    km = KernelManager(kernel_state_dir)
    km.ensure_running()
    yield km
    km.shutdown()


@pytest.fixture(scope="module")
def executor(kernel, kernel_state_dir):
    cache = CacheStore(kernel_state_dir / "cache")
    figures_dir = kernel_state_dir / "figures"
    figures_dir.mkdir(exist_ok=True)
    return Executor(
        kernel=kernel,
        cache=cache,
        figures_dir=figures_dir,
        working_dir=kernel_state_dir,
    )


def _run_cells(executor: Executor, cells: list[Cell]) -> dict:
    dag = DependencyGraph()
    dag.build(cells)
    scheduler = Scheduler()
    groups = scheduler.schedule(dag)
    cell_ids = {c.cell_id for c in cells}
    return executor.run(cells, groups, cell_ids)


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_simple_stdout(executor):
    cells = [_make_cell("c1", "print('hello typst_pyexec')")]
    results = _run_cells(executor, cells)
    assert "hello typst_pyexec" in results["c1"].stdout


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_expression_result(executor):
    cells = [_make_cell("c1", "2 + 2")]
    results = _run_cells(executor, cells)
    # Display data should contain the result
    stdout = results["c1"].stdout
    display = results["c1"].display_data
    assert "4" in stdout or any("4" in str(d) for d in display)


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_namespace_persistence(executor):
    """Kernel namespace should persist between cell executions."""
    cells = [
        _make_cell("c1", "persistent_var = 42", index=0),
        _make_cell("c2", "print(persistent_var)", index=1),
    ]
    results = _run_cells(executor, cells)
    assert "42" in results["c2"].stdout


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_error_captured(executor):
    cells = [_make_cell("err_cell", "raise ValueError('test error')")]
    results = _run_cells(executor, cells)
    r = results["err_cell"]
    assert r.status == "error"
    assert r.error is not None
    assert "ValueError" in r.error or "test error" in r.error


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_cache_hit_skips_execution(executor, kernel_state_dir):
    """Second build with identical cell should be served from cache."""
    cells = [_make_cell("cached_cell", "print('cached output')")]
    dag = DependencyGraph()
    dag.build(cells)
    groups = Scheduler().schedule(dag)

    # First run — executes
    results1 = executor.run(cells, groups, {"cached_cell"})
    assert not results1["cached_cell"].from_cache

    # Second run — cache should be hit (pass empty cells_to_run)
    results2 = executor.run(cells, groups, set())
    assert results2["cached_cell"].from_cache


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_changed_cell_reruns_in_warm_session(executor):
    """A changed cell must execute even if it already ran in this kernel session."""
    cells_v1 = [_make_cell("warm_rerun_cell", "print('version1')")]
    dag_v1 = DependencyGraph()
    dag_v1.build(cells_v1)
    groups_v1 = Scheduler().schedule(dag_v1)

    first = executor.run(cells_v1, groups_v1, {"warm_rerun_cell"}, dag=dag_v1)
    assert "version1" in first["warm_rerun_cell"].stdout

    cells_v2 = [_make_cell("warm_rerun_cell", "print('version2')")]
    dag_v2 = DependencyGraph()
    dag_v2.build(cells_v2)
    groups_v2 = Scheduler().schedule(dag_v2)

    second = executor.run(cells_v2, groups_v2, {"warm_rerun_cell"}, dag=dag_v2)
    assert second["warm_rerun_cell"].status == "ok"
    assert "version2" in second["warm_rerun_cell"].stdout
    assert not second["warm_rerun_cell"].from_cache


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_matplotlib_figure_saved(executor, kernel_state_dir):
    code = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[1,4,9])\n"
        "plt.show()\n"
    )
    cells = [_make_cell("fig_cell", code)]
    results = _run_cells(executor, cells)
    r = results["fig_cell"]
    assert len(r.figures) >= 1
    p = Path(r.figures[0])
    assert p.exists()
    assert p.suffix in (".svg", ".png")


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_matplotlib_figure_not_saved_without_show(executor, kernel_state_dir):
    code = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[1,4,9])\n"
    )
    cells = [_make_cell("fig_cell_no_show", code)]
    results = _run_cells(executor, cells)
    r = results["fig_cell_no_show"]
    assert len(r.figures) == 0


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_reactive_only_affected_cells_rerun(executor, kernel_state_dir):
    """Changing cell_1 should mark cell_2 (dependent) for re-run but not cell_3."""
    code_a = "x = 10"
    code_b = "y = x + 1"
    code_c = "z = 99"

    cells = [
        _make_cell("ca", code_a, 0),
        _make_cell("cb", code_b, 1),
        _make_cell("cc", code_c, 2),
    ]
    dag = DependencyGraph()
    dag.build(cells)
    affected = dag.affected({"ca"})

    assert "ca" in affected
    assert "cb" in affected
    assert "cc" not in affected


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_add_new_cell_no_reexecution_of_previous(executor, kernel_state_dir):
    """Adding a new independent cell must not invalidate cached previous cells."""
    cells_v1 = [_make_cell("p1", "import sys\nprint(sys.version)")]
    dag = DependencyGraph()
    dag.build(cells_v1)
    groups = Scheduler().schedule(dag)

    # First run
    executor.run(cells_v1, groups, {"p1"})

    # Add an independent cell
    cells_v2 = cells_v1 + [_make_cell("p2", "print('new cell')", index=1)]
    dag2 = DependencyGraph()
    dag2.build(cells_v2)
    groups2 = Scheduler().schedule(dag2)

    # p1 hash unchanged → should be served from cache
    results = executor.run(cells_v2, groups2, {"p2"})
    assert results["p1"].from_cache
    assert not results["p2"].from_cache


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_dependent_cell_hydrates_predecessors_after_kernel_reset(tmp_path):
    """Dependent cells should replay prerequisites into namespace when needed."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = state_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    kernel = KernelManager(state_dir)
    cache = CacheStore(state_dir / "cache")
    executor = Executor(
        kernel=kernel,
        cache=cache,
        figures_dir=figures_dir,
        working_dir=tmp_path,
    )

    cells = [
        _make_cell("c1", "directional_changes_returns = [1, 2, 3]", 0),
        _make_cell("c2", "print(directional_changes_returns[0])", 1),
    ]
    dag = DependencyGraph()
    dag.build(cells)
    groups = Scheduler().schedule(dag)

    try:
        # Prime cache and namespace.
        first = executor.run(cells, groups, {"c1", "c2"}, dag=dag)
        assert first["c2"].status == "ok"

        # Simulate a reconnected session where prerequisite names are missing.
        # Keep runtime imports intact, but remove dependency variable and clear
        # executor-local "executed in this session" tracking.
        kernel.execute("del directional_changes_returns")
        kernel._has_namespace_state = True  # type: ignore[attr-defined]
        executor._executed_cells.clear()  # type: ignore[attr-defined]

        # Re-executing only dependent cell must hydrate c1 first.
        second = executor.run(cells, groups, {"c2"}, dag=dag)
        assert second["c2"].status == "ok"
        assert "1" in second["c2"].stdout
        assert not second["c1"].from_cache
    finally:
        kernel.shutdown()


@pytest.mark.skipif(SKIP, reason="Kernel tests disabled")
def test_builder_rebuild_keeps_warm_namespace_without_replaying_prerequisites(tmp_path):
    """A warm watch-session rebuild should not re-run unchanged prerequisites."""
    source = tmp_path / "doc.typ"

    v1 = """```python
if "session_counter" not in globals():
    session_counter = 0
session_counter += 1
print(f"counter={session_counter}")
```

```python
print(f"value={session_counter}")
```
"""
    v2 = """```python
if "session_counter" not in globals():
    session_counter = 0
session_counter += 1
print(f"counter={session_counter}")
```

```python
# touch second cell hash
print(f"value={session_counter}")
```
"""

    source.write_text(v1, encoding="utf-8")
    builder = Builder(source=source, output_dir=tmp_path, compiler="typst")

    try:
        builder.build(compile_document=False)

        source.write_text(v2, encoding="utf-8")
        builder.build(compile_document=False)

        c1_hash = builder._cache.latest_hash("cell_1")
        c2_hash = builder._cache.latest_hash("cell_2")
        c1_entry = builder._cache.load_by_hash(c1_hash) if c1_hash else None
        c2_entry = builder._cache.load_by_hash(c2_hash) if c2_hash else None

        assert c1_entry is not None
        assert c2_entry is not None
        assert "counter=1" in c1_entry.get("stdout", "")
        assert "value=1" in c2_entry.get("stdout", "")
    finally:
        builder._kernel.shutdown()
