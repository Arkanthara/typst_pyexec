"""Tests for typst_pyexec.core.dag."""

from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.parser import Cell


def _make_cell(cell_id: str, source: str, index: int = 0) -> Cell:
    return Cell(cell_id=cell_id, index=index, source=source, start=0, end=len(source))


def _build(sources: dict[str, str]) -> DependencyGraph:
    cells = [_make_cell(cid, src, i) for i, (cid, src) in enumerate(sources.items())]
    dag = DependencyGraph()
    dag.build(cells)
    return dag


def test_no_dependencies():
    dag = _build({"c1": "a = 1", "c2": "b = 2"})
    assert dag.predecessors("c1") == set()
    assert dag.predecessors("c2") == set()


def test_linear_dependency():
    dag = _build(
        {
            "c1": "a = 10",
            "c2": "b = a + 1",
            "c3": "c = b * 2",
        }
    )
    assert dag.predecessors("c2") == {"c1"}
    assert dag.predecessors("c3") == {"c2"}
    assert dag.predecessors("c1") == set()


def test_reverse_dependency():
    dag = _build(
        {
            "c1": "a = 10",
            "c2": "b = a + 1",
        }
    )
    assert dag.successors("c1") == {"c2"}
    assert dag.successors("c2") == set()


def test_affected_transitive():
    dag = _build(
        {
            "c1": "a = 10",
            "c2": "b = a + 1",
            "c3": "c = b * 2",
            "c4": "d = 99",  # independent
        }
    )
    affected = dag.affected({"c1"})
    assert "c1" in affected
    assert "c2" in affected
    assert "c3" in affected
    assert "c4" not in affected


def test_affected_mid_chain():
    dag = _build(
        {
            "c1": "a = 10",
            "c2": "b = a + 1",
            "c3": "c = b * 2",
        }
    )
    affected = dag.affected({"c2"})
    assert "c1" not in affected
    assert "c2" in affected
    assert "c3" in affected


def test_topological_order_respects_deps():
    dag = _build(
        {
            "c1": "a = 10",
            "c2": "b = a + 1",
            "c3": "c = b * 2",
        }
    )
    order = dag.topological_order()
    assert order.index("c1") < order.index("c2")
    assert order.index("c2") < order.index("c3")


def test_import_counted_as_define():
    dag = _build(
        {
            "c1": "import numpy as np",
            "c2": "arr = np.arange(5)",
        }
    )
    info = dag.cell_info("c1")
    assert "np" in info.defines
    assert dag.predecessors("c2") == {"c1"}


def test_independent_cells_no_edge():
    dag = _build(
        {
            "c1": "x = 1",
            "c2": "y = 2",
            "c3": "z = x + y",
        }
    )
    # c3 depends on both c1 and c2
    assert "c1" in dag.predecessors("c3")
    assert "c2" in dag.predecessors("c3")
    # c1 and c2 are independent
    assert dag.predecessors("c1") == set()
    assert dag.predecessors("c2") == set()


def test_serialisation_roundtrip():
    dag = _build({"c1": "a = 1", "c2": "b = a + 1"})
    d = dag.to_dict()
    assert "c1" in d
    assert "c2" in d
    assert "c1" in d["c2"]["deps"]
