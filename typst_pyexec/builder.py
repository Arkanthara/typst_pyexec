"""High-level build orchestrator.

Ties together parsing, DAG analysis, scheduling, execution,
rendering, and document injection.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from typst_pyexec.core.cache import CacheStore
from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.executor import Executor
from typst_pyexec.core.kernel import KernelManager
from typst_pyexec.core.parser import Cell, Parser
from typst_pyexec.core.renderer import Renderer
from typst_pyexec.core.scheduler import Scheduler
from typst_pyexec.utils.hashing import sha256_text

logger = logging.getLogger(__name__)


class Builder:
    """Orchestrates the full build pipeline for a single Typst document.

    Parameters
    ----------
    source:
        Path to the ``.typ`` source file.
    output_dir:
        Directory in which to write the intermediate ``.typst_pyexec.typ``
        file and the ``.typst_pyexec/`` state directory.  Defaults to the
        directory of *source*.
    use_cache:
        Whether to use the disk-based execution cache.
    n_jobs:
        Number of parallel workers passed to ``joblib``.
    compiler:
        Name / path of the ``typst`` compiler binary.
    """

    def __init__(
        self,
        source: Path,
        output_dir: Path | None = None,
        use_cache: bool = True,
        n_jobs: int = -1,
        compiler: str = "typst",
    ) -> None:
        self.source = source.resolve()
        self.output_dir = (output_dir or self.source.parent).resolve()
        self.use_cache = use_cache
        self.n_jobs = n_jobs
        self.compiler = compiler

        self._state_dir = self.output_dir / ".typst_pyexec"
        self._figures_dir = self._state_dir / "figures"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._figures_dir.mkdir(parents=True, exist_ok=True)

        self._intermediate = self.output_dir / (self.source.stem + ".typst_pyexec.typ")

        self._cache = CacheStore(self._state_dir / "cache")
        self._kernel = KernelManager(self._state_dir)
        self._renderer = Renderer(self._figures_dir, self._state_dir)

        self._parser = Parser()
        self._dag = DependencyGraph()
        self._scheduler = Scheduler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Run a single build pass."""
        t0 = time.perf_counter()
        logger.info("Building %s", self.source)

        with _pushd(self.source.parent):
            source_text = self.source.read_text(encoding="utf-8")
            cells = self._parser.parse(source_text)

            if not cells:
                logger.info("No Python cells found — copying source verbatim.")
                self._intermediate.write_text(source_text, encoding="utf-8")
                self._compile()
                return

            self._dag.build(cells)
            groups = self._scheduler.schedule(self._dag)
            cell_hashes = self._compute_cell_hashes(cells)

            executor = Executor(
                kernel=self._kernel,
                cache=self._cache if self.use_cache else None,
                figures_dir=self._figures_dir,
                working_dir=self.source.parent,
                n_jobs=self.n_jobs,
            )

            changed_ids = self._detect_changed_cells(cells, cell_hashes)
            refresh_ids = {
                c.cell_id
                for c in cells
                if _as_bool(c.metadata.get("refresh"), False)
                and _as_bool(c.metadata.get("execute"), True)
            }
            # Regular changes cascade through DAG; refresh-only cells re-run
            # without cascading to downstream dependents.
            cells_to_run = self._dag.affected(changed_ids) | refresh_ids
            logger.info("%d/%d cells need execution", len(cells_to_run), len(cells))

            results = executor.run(
                cells,
                groups,
                cells_to_run,
                dag=self._dag,
                cell_hashes=cell_hashes,
            )

            # Render and inject
            output_text = self._renderer.inject(source_text, cells, results)
            self._intermediate.write_text(output_text, encoding="utf-8")

            # Persist notebooks for both normal replay and figure-export replay.
            self._renderer.sync_notebooks(cells, results, working_dir=self.source.parent)

            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("Build finished in %.1f ms", elapsed)

            self._compile()

    def watch(self) -> None:
        """Watch the source file and rebuild on every change."""
        from watchdog.events import FileModifiedEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        logger.info("Watching %s  (Ctrl-C to stop)", self.source)

        # Initial build
        try:
            self.build()
        except Exception as exc:
            logger.error("Initial build failed: %s", exc)

        class _Handler(FileSystemEventHandler):
            def __init__(self, builder: Builder) -> None:
                self._builder = builder

            def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
                event_path = Path(os.fsdecode(event.src_path)).resolve()
                if event_path == self._builder.source:
                    logger.info("Change detected — rebuilding…")
                    try:
                        self._builder.build()
                    except Exception as exc:
                        logger.error("Rebuild failed: %s", exc)

        observer = Observer()
        observer.schedule(_Handler(self), str(self.source.parent), recursive=False)
        observer.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("Stopping watcher…")
        finally:
            observer.stop()
            observer.join()
            self._kernel.shutdown()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_cell_hashes(self, cells: list[Cell]) -> dict[str, str]:
        """Return source hashes for executable cells."""
        return {
            cell.cell_id: sha256_text(cell.source)
            for cell in cells
            if _as_bool(cell.metadata.get("execute"), True)
        }

    def _detect_changed_cells(
        self, cells: list[Cell], cell_hashes: dict[str, str] | None = None
    ) -> set[str]:
        """Return IDs of cells whose source hash is not present in cache."""
        hashes = cell_hashes or self._compute_cell_hashes(cells)
        return {
            cell.cell_id
            for cell in cells
            if _as_bool(cell.metadata.get("execute"), True)
            and self._cache.load_by_hash(hashes[cell.cell_id]) is None
        }

    def _compile(self) -> None:
        """Invoke the Typst compiler on the intermediate document."""
        cmd = [self.compiler, "compile", str(self._intermediate)]
        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error(
                    "Typst compiler error:\n%s", result.stderr or result.stdout
                )
            else:
                logger.info("Compilation successful.")
        except FileNotFoundError:
            logger.warning(
                "Compiler %r not found — skipping compilation step.", self.compiler
            )
        except subprocess.TimeoutExpired:
            logger.error("Typst compiler timed out.")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def _pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
