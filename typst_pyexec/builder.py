"""High-level build orchestrator.

Ties together parsing, DAG analysis, scheduling, execution,
rendering, and document injection.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from typst_pyexec.core.cache import CacheStore
from typst_pyexec.core.dag import DependencyGraph
from typst_pyexec.core.executor import Executor
from typst_pyexec.core.kernel import KernelManager
from typst_pyexec.core.parser import Cell, Parser
from typst_pyexec.core.renderer import Renderer
from typst_pyexec.core.scheduler import Scheduler
from typst_pyexec.utils.hashing import sha256_text
from typst_pyexec.utils.options import parse_bool

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

    def build(self, *, compile_document: bool = True) -> None:
        """Run a single build pass."""
        t0 = time.perf_counter()
        logger.info("Building %s", self.source)

        with _pushd(self.source.parent):
            source_text = self.source.read_text(encoding="utf-8")
            cells = self._parser.parse(source_text)

            if not cells:
                logger.info("No Python cells found — copying source verbatim.")
                self._intermediate.write_text(source_text, encoding="utf-8")
                if compile_document:
                    self._compile()
                return

            self._dag.build(cells)
            groups = self._scheduler.schedule(self._dag)
            cell_hashes = self._compute_cell_hashes(cells) if self.use_cache else None

            executor = Executor(
                kernel=self._kernel,
                cache=self._cache if self.use_cache else None,
                figures_dir=self._figures_dir,
                working_dir=self.source.parent,
                n_jobs=self.n_jobs,
            )

            executable_ids = {
                c.cell_id
                for c in cells
                if parse_bool(c.metadata.get("execute"), True)
            }
            if self.use_cache:
                changed_ids = self._detect_changed_cells(cells, cell_hashes)
            else:
                changed_ids = set(executable_ids)

            refresh_ids = {
                c.cell_id
                for c in cells
                if c.cell_id in executable_ids
                and parse_bool(c.metadata.get("refresh"), False)
            }
            # Regular changes cascade through DAG; refresh-only cells re-run
            # without cascading to downstream dependents.
            affected_ids = self._dag.affected(changed_ids)
            cells_to_run = affected_ids | refresh_ids
            if changed_ids:
                logger.info("Changed executable cells: %s", ", ".join(sorted(changed_ids)))
            if affected_ids and affected_ids != changed_ids:
                logger.info(
                    "Dependent cells scheduled due to DAG: %s",
                    ", ".join(sorted(affected_ids - changed_ids)),
                )
            if refresh_ids:
                logger.info("Refresh-forced cells: %s", ", ".join(sorted(refresh_ids)))
            logger.info("%d/%d cells need execution", len(cells_to_run), len(cells))
            if not self.use_cache:
                logger.info(
                    "Cache disabled; executing all %d executable cell(s).",
                    len(executable_ids),
                )
            elif not cells_to_run and cells:
                logger.info(
                    "No Python source hash changed; all executable cells are cache hits. "
                    "If you expected re-execution, ensure the code content changed or set %%| refresh: true on that cell."
                )

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

            if compile_document:
                self._compile()

    def watch(
        self,
        *,
        preview_engine: Literal["auto", "tinymist", "typst", "none"] = "auto",
    ) -> None:
        """Watch source changes and regenerate the intermediate document.

        In watch mode, builds only refresh ``*.typst_pyexec.typ``.
        Live rendering is delegated to a long-running preview process.
        """
        from watchdog.events import FileModifiedEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        logger.info("Watching %s  (Ctrl-C to stop)", self.source)

        # Initial build
        initial_build_ok = False
        try:
            self.build(compile_document=False)
            initial_build_ok = True
        except Exception as exc:
            logger.error("Initial build failed: %s", exc)

        preview_state: dict[str, subprocess.Popen[str] | None] = {"process": None}
        if initial_build_ok:
            preview_state["process"] = self._start_preview(preview_engine)
        else:
            logger.warning(
                "Live preview not started because initial build failed; it will start after the next successful rebuild."
            )

        class _Handler(FileSystemEventHandler):
            def __init__(self, builder: Builder) -> None:
                self._builder = builder

            def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
                event_path = Path(os.fsdecode(event.src_path)).resolve()
                if event_path == self._builder.source:
                    logger.info("Change detected — rebuilding…")
                    try:
                        self._builder.build(compile_document=False)
                        if preview_state["process"] is None:
                            preview_state["process"] = self._builder._start_preview(
                                preview_engine
                            )
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
            self._stop_preview(preview_state["process"])
            self._kernel.shutdown()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_cell_hashes(self, cells: list[Cell]) -> dict[str, str]:
        """Return source hashes for executable cells."""
        return {
            cell.cell_id: sha256_text(cell.source)
            for cell in cells
            if parse_bool(cell.metadata.get("execute"), True)
        }

    def _detect_changed_cells(
        self, cells: list[Cell], cell_hashes: dict[str, str] | None = None
    ) -> set[str]:
        """Return IDs of cells whose source changed since previous build.

        A cell is considered changed when its current hash differs from the
        most recently recorded hash for the same ``cell_id``. If this is a
        new ``cell_id`` (for example after reordering), we still reuse cache
        by source hash when available.
        """
        hashes = cell_hashes or self._compute_cell_hashes(cells)
        latest_by_id = {
            cell.cell_id: self._cache.latest_hash(cell.cell_id)
            for cell in cells
            if parse_bool(cell.metadata.get("execute"), True)
        }
        latest_hashes_in_doc = {h for h in latest_by_id.values() if h is not None}
        changed: set[str] = set()
        for cell in cells:
            if not parse_bool(cell.metadata.get("execute"), True):
                continue

            current_hash = hashes[cell.cell_id]
            last_hash = latest_by_id.get(cell.cell_id)

            # Existing cell ID: compare to previous build's hash.
            if last_hash is not None:
                if last_hash != current_hash:
                    # If this hash is already the latest hash of another cell
                    # in the current document, this is most likely a cell ID
                    # shift/reorder rather than new code.
                    if current_hash not in latest_hashes_in_doc:
                        changed.add(cell.cell_id)
                continue

            # New/shifted cell ID: only run if source hash has never been seen.
            if self._cache.load_by_hash(current_hash) is None:
                changed.add(cell.cell_id)

        return changed

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

    def _resolve_preview_command(
        self, preview_engine: Literal["auto", "tinymist", "typst", "none"]
    ) -> list[str] | None:
        if preview_engine == "none":
            return None

        if preview_engine in {"auto", "tinymist"} and shutil.which("tinymist"):
            return ["tinymist", "preview", str(self._intermediate)]

        if preview_engine == "tinymist":
            logger.warning(
                "tinymist requested but not found on PATH; falling back to typst watch."
            )

        return [self.compiler, "watch", str(self._intermediate)]

    def _start_preview(
        self,
        preview_engine: Literal["auto", "tinymist", "typst", "none"],
    ) -> subprocess.Popen[str] | None:
        cmd = self._resolve_preview_command(preview_engine)
        if cmd is None:
            logger.info("Preview disabled; only regenerating %s", self._intermediate)
            return None

        logger.info("Starting live preview: %s", " ".join(cmd))
        try:
            process = subprocess.Popen(cmd, cwd=str(self.output_dir), text=True)
        except FileNotFoundError:
            logger.warning(
                "Preview command not found (%r). Continuing without live preview.", cmd[0]
            )
            return None

        # tinymist can fail immediately (e.g. port already in use).
        # If that happens, fall back to typst watch when allowed.
        if cmd[0] == "tinymist":
            time.sleep(0.2)
            returncode = process.poll()
            if returncode is not None:
                logger.warning(
                    "tinymist preview exited immediately (code=%s). Falling back to typst watch.",
                    returncode,
                )
                fallback = [self.compiler, "watch", str(self._intermediate)]
                logger.info("Starting live preview fallback: %s", " ".join(fallback))
                try:
                    return subprocess.Popen(
                        fallback,
                        cwd=str(self.output_dir),
                        text=True,
                    )
                except FileNotFoundError:
                    logger.warning(
                        "Fallback preview command not found (%r). Continuing without live preview.",
                        fallback[0],
                    )
                    return None

        return process

    def _stop_preview(self, process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

@contextmanager
def _pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
