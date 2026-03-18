# typst_pyexec

typst_pyexec is a reactive Python execution engine for Typst documents.

It executes Python fences inside `.typ` files, captures outputs (stdout, figures, tables), and injects rendered Typst markup into an intermediate file (`*.typst_pyexec.typ`) that can be compiled directly.

## Core Capabilities

- Reactive dependency graph based on Python AST analysis
- Incremental execution with source-hash cache
- Reduced duplicate hashing and cache I/O in the execution pipeline
- Centralized metadata option parsing for consistent behavior across builder, executor, and renderer
- Persistent Jupyter kernel between builds
- Automatic cold-kernel hydration for dependency prerequisites
- Matplotlib figure export to SVG with PNG fallback
- Subfigure reconstruction via `figure(grid(...))`
- DataFrame HTML rendering to Typst `#table(...)`
- Watch mode with automatic rebuilds and live preview (`tinymist preview` preferred)

## Installation

```bash
pip install typst_pyexec
```

For development:

```bash
uv sync --extra dev
```

## CLI Usage

Build once:

```bash
typst_pyexec build document.typ
```

Watch and rebuild on save:

```bash
typst_pyexec watch document.typ
```

Watch with explicit preview backend:

```bash
typst_pyexec watch document.typ --preview-engine auto
```

Clean local state directory:

```bash
typst_pyexec clean
```

Common options:

- `--output-dir <dir>`: write intermediate/state outputs to a custom folder
- `--no-cache`: disable cache lookups and force execution
- `--jobs <n>`: reserved for future multi-kernel scheduling (`-1` default)
- `--compiler <cmd>`: Typst compiler binary name/path (default `typst`)
- `watch --preview-engine <auto|tinymist|typst|none>`: select live preview backend

Watch mode behavior:

- Watch mode regenerates `*.typst_pyexec.typ` on each save and delegates rendering to a live preview process.
- `--preview-engine auto` tries `tinymist preview` first (if `tinymist` is on PATH), then falls back to `typst watch`.
- `--preview-engine tinymist` prefers `tinymist preview`; if `tinymist` is unavailable it falls back to `typst watch`.
- `--preview-engine typst` always runs `typst watch`.
- `--preview-engine none` disables live preview and only refreshes the intermediate file.

## Block Options (`%|`)

Place options at the top of a Python fence:

````typst
```python
%| echo: false
%| raw: true
%| figure: true
%| plt-lines.linewidth: 0.8
%| plt-axes.linewidth: 0.6
print("hello")
```
````

Supported options:

- `execute` (default `true`): skip execution when `false`
- `refresh` (default `false`): force this cell to run every build
- `echo` (default `true`): show or hide source code
- `raw` (default `true`): show or hide textual runtime output (`stdout`, traceback, `text/plain` display bundles)
- `figure` (default `true`): show or hide rendered figures
- `caption`: explicit figure caption override
- `label`: Typst label for cross-references
- `keep-subplots` (default `false`): preserve multi-axis plot as one image
- `img-*`: passthrough kwargs for Typst `image(...)`
- `fig-*`: passthrough kwargs for Typst `figure(...)`
- `grid-*`: passthrough kwargs for Typst `grid(...)`
- `plt-*`: per-cell matplotlib `rcParams` overrides (key after `plt-` maps to rcParam name)

Default `plt-*` values applied to every executed cell:

- `lines.linewidth: 0.8`
- `axes.linewidth: 0.6`
- `grid.linewidth: 0.2`
- `axes.grid: true`
- `lines.markersize: 4` (scatter points are ~2x smaller in area than matplotlib default)

`plt-*` examples:

- `%| plt-lines.linewidth: 0.8`
- `%| plt-axes.linewidth: 0.6`
- `%| plt-grid.linewidth: 0.4`
- `%| plt-axes.grid: true`

Precedence order for plot style values:

1. Built-in defaults (values above)
2. `%| plt-*` metadata options
3. Any `matplotlib.rcParams[...] = ...` or `plt.rcParams.update(...)` in Python code

This means Python code always wins over `%| plt-*`, and `%| plt-*` wins over defaults.

Behavior notes:

- `cell_id` is internal and generated automatically.
- Python fences can be indented to fit paragraph/layout context: shared left padding is automatically removed before execution.
- In generated `.typst_pyexec.typ`, that original block padding is preserved for both rendered source and rendered outputs.
- Option-only edits (including `plt-*`) do not invalidate cache because cache keys are based on Python source.
- To re-run on option updates, set `refresh: true`.
- Build logs include `effective plot rcParams` per executed cell for quick verification.

## Refactor Notes

- Shared option parsing lives in `typst_pyexec/utils/options.py`, removing duplicated boolean parsing logic.
- Executor cache handling now uses one code path to validate and materialize cached results, reducing branching duplication.
- Release workflow artifact upload now targets the full `dist/` directory and fails clearly if artifacts are missing.

## Figure and Caption Behavior

- For single-axis plots, title text is promoted into Typst caption when `caption` is not set.
- For subplot grids, each axis title becomes child caption; suptitle becomes outer caption.
- Title text is removed from exported images after promotion to captions.
- If SVG export fails and PNG is emitted, renderer auto-resolves PNG paths in final Typst output.

## How It Works

1. Parse Python fences from Typst source
2. Build def/use DAG from AST
3. Compute changed cells from source-hash cache
4. Execute required cells in topological groups
5. Capture stdout, display bundles, and figure artifacts
6. Render Typst fragments per cell
7. Inject into `document.typst_pyexec.typ`
8. Build mode: compile with Typst compiler
9. Watch mode: refresh intermediate file and stream live preview via `tinymist preview` or `typst watch`

## Local State

typst_pyexec writes runtime state into `.typst_pyexec/`:

- `kernel_connection.json`: reconnect data for persistent kernel
- `cache/`: JSON entries keyed by SHA-256 of cell source
- `figures/`: SVG/PNG artifacts
- `notebook.ipynb`: synchronized notebook in normal execution mode (original user cells)
- `notebook_export.ipynb`: synchronized notebook in export mode (figure capture preamble/postamble included)

### Notebook Modes

Two notebooks are intentionally generated:

- `notebook.ipynb` preserves the authored Python cells and attached outputs exactly as written (minimal overhead).
- `notebook_export.ipynb` wraps each cell with figure-export helpers that invoke optimized functions.

This split keeps one notebook readable for normal analysis and reproducibility, while the export notebook is designed for generating Typst-integrated figures and results.

**Standalone Execution**: Both notebooks include a setup cell that initializes the environment:
- **Normal mode**: Sets working directory for relative path consistency
- **Export mode**: Sets working directory AND imports matplotlib, pyplot, and the `save_figures_and_metadata` function—enabling the export notebook to execute standalone without additional setup

**Performance**: Both notebooks initialize matplotlib and import optimized figure management routines once per kernel session. This eliminates per-cell code injection overhead compared to earlier versions (~2-3KB per cell reduced to ~500 bytes per cell).

## Development Quality Gates

Run checks locally:

```bash
uv run ruff check .
uv run black --check typst_pyexec tests
uv run mypy typst_pyexec
uv run pytest
```

## CI/CD (GitHub Actions)

- `CI`: matrix on Ubuntu and Windows, Python 3.10-3.12, with lint + format + type-check + tests + coverage artifact
- `Release`: tag-driven (`v*.*.*`) build and publish workflow for PyPI using trusted publishing

Workflows are in:

- `.github/workflows/ci.yml`
- `.github/workflows/release.yml`

## Publish to PyPI

typst_pyexec is already configured for trusted publishing through GitHub Actions.

Release steps:

1. Bump version in `pyproject.toml` and `typst_pyexec/__init__.py`.
2. Commit and push to main.
3. Create and push a version tag:

```bash
git tag v0.1.1
git push origin v0.1.1
```

4. GitHub Actions runs `.github/workflows/release.yml` and publishes to PyPI.

Pre-tag checklist:

1. `python -m pytest -q` is green.
2. `python -m build` succeeds.
3. `python -m twine check dist/*` passes.
4. Version matches in `pyproject.toml` and `typst_pyexec/__init__.py`.
5. Git tag matches that version (`vX.Y.Z`).

Local preflight checks before tagging:

```bash
python -m build
python -m twine check dist/*
```

If you prefer token-based manual publishing:

```bash
python -m twine upload dist/*
```

## Use in Other Repositories

After publishing, install as a normal dependency.

With pip:

```bash
pip install typst_pyexec
```

With uv (project dependency):

```bash
uv add typst_pyexec
```

With uvx (run CLI without adding dependency):

```bash
uvx typst_pyexec build document.typ
```

## License

MIT
