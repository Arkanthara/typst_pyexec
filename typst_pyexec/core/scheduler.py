"""DAG-based execution scheduler.

Produces ordered groups of cells that can safely run in parallel
within each group.
"""

from __future__ import annotations

import logging
from collections import deque

from typst_pyexec.core.dag import DependencyGraph

logger = logging.getLogger(__name__)

# A group is a list of cell_ids that may run concurrently.
ExecutionGroup = list[str]


class Scheduler:
    """Convert a :class:`DependencyGraph` into parallel execution groups.

    Uses Kahn's BFS-based topological sort, collecting nodes whose
    in-degree drops to zero simultaneously into the same group.

    Example
    -------
    For a graph A→B, C→D the result is::

        [[A, C], [B, D]]

    meaning A and C can run in parallel, then B and D.
    """

    def schedule(self, dag: DependencyGraph) -> list[ExecutionGroup]:
        """Return a list of parallel execution groups in dependency order.

        Parameters
        ----------
        dag:
            The dependency graph produced by :class:`DependencyGraph`.

        Returns
        -------
        list[ExecutionGroup]
            Each inner list contains cell IDs that are mutually independent
            and may run concurrently.  Groups are ordered so that all
            dependencies of a cell in group *i* appear in groups *< i*.
        """
        # Build in-degree table using only DAG edges
        ids = dag.all_ids()
        in_degree: dict[str, int] = {cid: 0 for cid in ids}
        for cid in ids:
            for _dep in dag.predecessors(cid):
                in_degree[cid] = in_degree.get(cid, 0) + 1

        queue: deque[str] = deque(cid for cid, deg in in_degree.items() if deg == 0)
        groups: list[ExecutionGroup] = []

        while queue:
            # All nodes currently in queue have zero unresolved dependencies
            group = list(queue)
            groups.append(group)
            queue.clear()

            for cid in group:
                for succ in dag.successors(cid):
                    in_degree[succ] -= 1
                    if in_degree[succ] == 0:
                        queue.append(succ)

        executed = sum(len(g) for g in groups)
        if executed != len(ids):
            logger.warning(
                "Cycle detected in dependency graph — %d cells could not be scheduled.",
                len(ids) - executed,
            )

        logger.debug("Scheduled %d groups: %s", len(groups), groups)
        return groups
