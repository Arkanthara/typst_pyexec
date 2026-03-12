# typst-py

A single-file Python preprocessor that executes fenced `python` blocks in [Typst](https://typst.app) documents and replaces them with rendered output.

## Requirements

| Tool | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Run the script |
| [typst](https://github.com/typst/typst) | Compile mode (`-c`) |
| [tinymist](https://github.com/Myriad-Dreamin/tinymist) | Watch mode (`-w`) live preview |
| matplotlib *(optional)* | Figure capture from Python blocks |
| dill *(optional)* | Faster re-execution via namespace snapshots |

`typst_pyexecutor.py` uses the Python standard library only. If matplotlib is unavailable, code execution still works but figure capture is disabled. If dill is unavailable, pickle is used as a fallback for namespace snapshots (with reduced coverage of serializable object types).

## Quickstart

```bash
# Compile once
uv run --with matplotlib typst_pyexecutor.py -c report.typ

# Watch mode (preprocess on save + tinymist live preview)
uv run --with matplotlib typst_pyexecutor.py -w report.typ

# With namespace snapshots for faster re-execution
uv run --with matplotlib --with dill typst_pyexecutor.py -w report.typ
```

Add extra Python packages your code blocks need with additional `--with` flags:

```bash
uv run --with numpy --with pandas --with matplotlib --with dill typst_pyexecutor.py -c report.typ
```

## How It Works

1. Read the `.typ` file and find each ` ```python ... ``` ` fenced block.
2. Execute all blocks in one shared namespace.
3. Replace each block with:
   - source listing (unless `%| echo: false`)
   - printed output (when `execute: true`)
   - matplotlib figures (saved as PNG, inserted as `#figure`/`#grid`)

Generated artifacts are written under `.typst_py/` next to the source file.

## Caching And Re-Execution

Each block is hashed by its Python code (SHA-256) and its display options (SHA-256) separately.

**Three cache outcomes per block:**

| Situation | Action | Performance |
|---|---|---|
| Code + options unchanged | Use cached output directly | Instant |
| Code unchanged, options changed | Re-render from cached execution data | Instant (no Python) |
| Code changed | Re-execute Python | Snapshot-accelerated |

### Namespace Snapshots

When a block's code changes, all subsequent blocks must re-execute because the shared namespace may have changed. The **naive approach** replays every earlier block from scratch to rebuild the namespace — this is the main bottleneck.

**typst_pyexecutor.py** saves a namespace snapshot (serialized with `dill` or `pickle`) after each block execution. When a block needs re-execution:

1. Load the snapshot from the **previous** block (skipping all earlier replay).
2. If that snapshot is missing, scan backward for the nearest available one.
3. Replay only the blocks between the snapshot and the target.
4. If no snapshot exists at all, replay from a fresh namespace.

**Result:** Editing block 50 in a 100-block document restores from snapshot 49 instead of replaying blocks 1–49.

> **dill recommended:** `dill` can serialize modules, lambdas, closures, and most Python objects. Standard `pickle` handles common scientific objects (numpy arrays, dataframes) but may fail on modules imported inside code blocks. If serialization fails, the system falls back to replay automatically.

## Block Options

Place `%|` option lines at the very top of a Python code block.

### Execution Control

| Option | Values | Default | Description |
|---|---|---|---|
| `execute` | `true` / `false` | `true` | Whether to run the code |
| `echo` | `true` / `false` | `true` | Whether to show the source listing |
| `refresh` | `true` / `false` | `false` | Force re-execution every run (no cascade) |
| `execute-all` | `true` / `false` | `false` | Re-execute all blocks from 1 through this one |

#### `refresh` vs `execute-all`

- **`refresh: true`** — Re-executes *only this block* on every run, regardless of cache. The namespace is restored from the previous block's snapshot. Downstream blocks are **not** automatically re-executed. Use this for blocks that depend on external state (files, databases, time, randomness).

- **`execute-all: true`** — Re-executes *all blocks from the beginning through this one* on every run. Downstream blocks **are** cascaded for re-execution since the namespace may have changed. Use this when you need to guarantee a completely fresh execution up to a certain point.

**Example:**

````typst
```python
%| refresh: true
# This block always runs, but blocks after it use their cache
data = load_from_database()
```

```python
%| execute-all: true
# This AND all blocks before it are re-executed from scratch
# Blocks after this one are also re-executed (cascade)
result = compute(data)
```
````

### Figure Behavior

| Option | Values | Default | Notes |
|---|---|---|---|
| `caption` | text | none | Explicit block caption |
| `label` | typst label name | none | Outer figure label `<name>` |
| `keep-title` | `true` / `false` | `false` | Keep matplotlib titles on images |
| `keep-subplots` | `true` / `false` | `false` | Keep multi-axes figures as one image |

### Typst Parameter Passthrough

All Typst arguments are passed via prefixes only:

| Prefix | Target call | Example |
|---|---|---|
| `img-xxx` | `image(xxx: ...)` | `%| img-width: 80%` |
| `fig-xxx` | `figure(xxx: ...)` | `%| fig-placement: top` |
| `grid-xxx` | `grid(xxx: ...)` | `%| grid-gutter: 1em` |

No default `image/figure/grid` options are injected unless required by the behaviors below.

## Figure And Grid Behavior

- Each axes is saved as a separate image by default.
- `%| keep-subplots: true` keeps the full matplotlib figure as one image.
- Multiple images from one block are always emitted as a Typst `grid(...)`.
- Subfigures default to `kind: "subfigure"`, `supplement: none`, `numbering: "(a)"`.
- Child labels use letter suffixes from block label:
  - `%| label: fig-1` → outer `<fig-1>`, children `<fig-1a>`, `<fig-1b>`, ...

### Title Conversion

Matplotlib titles are converted for Typst captions by removing backslashes and replacing `{}` with `()`.

### Subfigure Show Rules

To properly format subfigures, add these show rules to your Typst template:

```typst
#show figure.where(kind: "subfigure"): set figure(supplement: "Figure")

#show figure.where(kind: image): outer => {
  counter(figure.where(kind: "subfigure")).update(0)
  set figure(numbering: (..nums) => {
    let outer-nums = counter(figure.where(kind: image)).at(outer.location())
    std.numbering("1a", ..outer-nums, ..nums)
  })
  show figure.where(kind: "subfigure"): inner => {
    show figure.caption: it => context {
      strong(std.numbering("(a)", it.counter.at(inner.location()).last()))
      [ ]
      it.body
    }
    inner
  }
  outer
}
```

## Watch Mode

`-w` does:

1. Preprocess once.
2. Start `tinymist preview` on the generated `.typ` file (live HTML preview with auto-reload).
3. Poll source tree mtimes for changes.
4. Re-preprocess only when files are saved.

Namespace snapshots persist across re-preprocessing runs, so editing a single block in watch mode is fast even in large documents.

## CLI

```text
usage: typst_pyexecutor.py [-c | -w] [options] input.typ

  -c, --compile        Compile to PDF once
  -w, --watch          Watch for changes, preprocess on save, live preview

  -d, --debug          Keep intermediate generated .typ file after compile
  --images-dir PATH    Image directory inside .typst_py/ (default: img)
  --interval SEC       Polling interval in seconds for watch mode (default: 1.0)
```

## File Layout

```text
project/
├── report.typ
├── typst_pyexecutor.py
└── .typst_py/
    ├── report.generated.typ
    ├── block-cache.json
    ├── snapshots/
    │   ├── ns_0.pkl
    │   ├── ns_1.pkl
    │   └── ...
    └── img/
        ├── b1_f1.png
        └── ...
```

Add `.typst_py/` to `.gitignore`.

## Migration from typst_py.py

`typst_pyexecutor.py` is a drop-in replacement for `typst_py.py` with the same CLI interface and block option syntax. To migrate:

1. Replace `typst_py.py` with `typst_pyexecutor.py` in your commands.
2. Optionally add `--with dill` for namespace snapshot support.
3. Delete `.typst_py/` to start with a clean cache (recommended but not required).

New options `refresh` and `execute-all` are opt-in — existing documents work without changes.

## License

This project is licensed under the MIT License.
See `LICENSE` for the full text.
