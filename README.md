# typst_pyexec

typst_pyexec is a reactive Python execution engine for Typst documents.

It executes Python fences inside `.typ` files, captures outputs (stdout, figures, tables), and injects rendered Typst markup into an intermediate file (`*.typst_pyexec.typ`) that can be compiled directly.

## Core Capabilities

- Reactive dependency graph based on Python AST analysis
- Incremental execution with source-hash cache
- Persistent Jupyter kernel between builds
- Automatic cold-kernel hydration for dependency prerequisites
- Matplotlib figure export to SVG with PNG fallback
- Subfigure reconstruction via `figure(grid(...))`
- DataFrame HTML rendering to Typst `#table(...)`
- Watch mode with automatic rebuilds

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

Clean local state directory:

```bash
typst_pyexec clean
```

Common options:

- `--output-dir <dir>`: write intermediate/state outputs to a custom folder
- `--no-cache`: disable cache lookups and force execution
- `--jobs <n>`: reserved for future multi-kernel scheduling (`-1` default)
- `--compiler <cmd>`: Typst compiler binary name/path (default `typst`)

## Block Options (`%|`)

Place options at the top of a Python fence:

````typst
```python
%| echo: false
%| raw: true
%| figure: true
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

Behavior notes:

- `cell_id` is internal and generated automatically.
- Option-only edits do not invalidate cache because cache keys are based on Python source.
- To re-run on option updates, set `refresh: true`.

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
8. Compile with Typst compiler

## Local State

typst_pyexec writes runtime state into `.typst_pyexec/`:

- `kernel_connection.json`: reconnect data for persistent kernel
- `cache/`: JSON entries keyed by SHA-256 of cell source
- `figures/`: SVG/PNG artifacts
- `notebook.ipynb`: synchronized notebook representation

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
