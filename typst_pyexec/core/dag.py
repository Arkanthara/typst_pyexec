"""AST-based dependency analysis and DAG construction between cells."""

from __future__ import annotations

import ast
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from typst_pyexec.core.parser import Cell

logger = logging.getLogger(__name__)


@dataclass
class CellInfo:
    """Dependency metadata for one cell.

    Attributes
    ----------
    cell_id:
        Stable identifier.
    defines:
        Names assigned (written) in this cell.
    uses:
        Names read (used) in this cell.
    deps:
        IDs of cells that this cell depends on.
    """

    cell_id: str
    defines: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    deps: set[str] = field(default_factory=set)


class DependencyGraph:
    """Build and query a directed acyclic graph of cell dependencies.

    Edges represent "must execute before" relationships based on variable
    definitions and usages discovered through AST analysis.
    """

    def __init__(self) -> None:
        # cell_id → CellInfo
        self._info: dict[str, CellInfo] = {}
        # Reverse edges: cell_id → set of cell_ids that depend on it
        self._rdeps: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, cells: list[Cell]) -> None:
        """Analyse *cells* and (re)build the full dependency graph.

        A cell B depends on cell A when A defines a name that B uses,
        and A precedes B in document order.
        """
        self._info.clear()
        self._rdeps.clear()

        # First pass: gather defines/uses per cell
        infos: list[CellInfo] = []
        for cell in cells:
            info = _analyse_cell(cell)
            self._info[cell.cell_id] = info
            infos.append(info)

        # Second pass: wire up edges (only earlier cells can be deps)
        # "defined so far" maps name → most-recent defining cell_id
        defined_by: dict[str, str] = {}
        for info in infos:
            for name in info.uses:
                if name in defined_by:
                    dep_id = defined_by[name]
                    if dep_id != info.cell_id:
                        info.deps.add(dep_id)
                        self._rdeps[dep_id].add(info.cell_id)
            # Update definition map (later cell overrides earlier one)
            for name in info.defines:
                defined_by[name] = info.cell_id

        logger.debug("DAG built: %d cells, edges=%s", len(infos), self._edge_count())

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def predecessors(self, cell_id: str) -> set[str]:
        """Return direct dependency IDs of *cell_id*."""
        return self._info[cell_id].deps if cell_id in self._info else set()

    def successors(self, cell_id: str) -> set[str]:
        """Return IDs of cells that directly depend on *cell_id*."""
        return self._rdeps.get(cell_id, set())

    def affected(self, changed_ids: set[str]) -> set[str]:
        """Return the transitive closure of all cells affected by changes.

        Includes *changed_ids* themselves plus every cell reachable via
        forward edges (cells that depend on the changed cells).
        """
        visited: set[str] = set()
        queue = list(changed_ids)
        while queue:
            cid = queue.pop()
            if cid in visited:
                continue
            visited.add(cid)
            queue.extend(self._rdeps.get(cid, set()))
        return visited

    def required_with_predecessors(self, target_ids: set[str]) -> set[str]:
        """Return *target_ids* plus all transitive predecessors.

        Useful for hydrating a cold kernel namespace before executing only
        changed cells. If target cell ``C`` depends on ``B`` which depends on
        ``A``, this returns ``{A, B, C}``.
        """
        required: set[str] = set()
        queue = list(target_ids)
        while queue:
            cid = queue.pop()
            if cid in required:
                continue
            required.add(cid)
            queue.extend(self.predecessors(cid))
        return required

    def topological_order(self) -> list[str]:
        """Return all cell IDs in a valid topological execution order."""
        order: list[str] = []
        visited: set[str] = set()

        def _visit(cid: str) -> None:
            if cid in visited:
                return
            visited.add(cid)
            for dep in self._info[cid].deps:
                _visit(dep)
            order.append(cid)

        for cid in self._info:
            _visit(cid)
        return order

    def cell_info(self, cell_id: str) -> CellInfo | None:
        return self._info.get(cell_id)

    def all_ids(self) -> list[str]:
        return list(self._info.keys())

    # ------------------------------------------------------------------
    # Serialisation (for state.json)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            cid: {
                "defines": list(info.defines),
                "uses": list(info.uses),
                "deps": list(info.deps),
            }
            for cid, info in self._info.items()
        }

    def _edge_count(self) -> int:
        return sum(len(info.deps) for info in self._info.values())


# ---------------------------------------------------------------------------
# AST analysis helpers
# ---------------------------------------------------------------------------


def _analyse_cell(cell: Cell) -> CellInfo:
    """Return :class:`CellInfo` for *cell* by walking its AST."""
    info = CellInfo(cell_id=cell.cell_id)
    try:
        tree = ast.parse(cell.source, mode="exec")
    except SyntaxError as exc:
        logger.warning(
            "Cell %s: syntax error during DAG analysis: %s", cell.cell_id, exc
        )
        return info

    _DefUseVisitor(info).visit(tree)
    return info


class _DefUseVisitor(ast.NodeVisitor):
    """Collect top-level name definitions and usages."""

    def __init__(self, info: CellInfo) -> None:
        self._info = info
        self._scope_depth = 0

    # -- definitions -------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._collect_targets(target)
        self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._collect_targets(node.target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._collect_targets(node.target)
        if node.value:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._scope_depth == 0:
            self._info.defines.add(node.name)
        # Do not recurse into the body for top-level def tracking

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._scope_depth == 0:
            self._info.defines.add(node.name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name.split(".")[0]
            self._info.defines.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            if name != "*":
                self._info.defines.add(name)

    def visit_For(self, node: ast.For) -> None:
        self._collect_targets(node.target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars:
                self._collect_targets(item.optional_vars)
        self.generic_visit(node)

    # -- usages ------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._info.uses.add(node.id)

    # -- helpers -----------------------------------------------------------

    def _collect_targets(self, node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            self._info.defines.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self._collect_targets(elt)
        elif isinstance(node, ast.Starred):
            self._collect_targets(node.value)
        elif isinstance(node, ast.Attribute):
            self.visit(node.value)
        elif isinstance(node, ast.Subscript):
            self.visit(node.value)
            self.visit(node.slice)
