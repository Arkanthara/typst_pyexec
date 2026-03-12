# typst-py

A single-file Python preprocessor that executes fenced `python` blocks in [Typst](https://typst.app) documents and replaces them with rendered output — powered by a Jupyter kernel (ipykernel).

## Requirements

| Tool | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Run the script |
| [typst](https://github.com/typst/typst) | Compile mode (`-c`) |
| [tinymist](https://github.com/Myriad-Dreamin/tinymist) | Watch mode (`-w`) live preview |
| ipykernel | Jupyter kernel for code execution |
| matplotlib *(optional)* | Figure capture from Python blocks |

## Quickstart

```bash
# Compile once
uv run typst_ipy.py -c report.typ

# With a specific virtualenv (e.g. for project-specific packages)
uv run typst_ipy.py -c report.typ --python /path/to/venv/bin/python

# Watch mode (preprocess on save + tinymist live preview)
uv run typst_ipy.py -w report.typ
```

Add extra Python packages your code blocks need with additional `--with` flags:

```bash
uv run --with numpy --with pandas --with matplotlib typst_ipy.py -c report.typ
```

## How It Works

1. Read the `.typ` file and find each ` ```python ... ``` ` fenced block.
2. Start a Jupyter kernel (ipykernel) with a shared Python namespace.
3. Execute each block as a cell in the kernel.
4. Replace each block with:
   - source listing (unless `%| echo: false`)
   - printed output (when `execute: true`)
   - matplotlib figures (saved as PNG, inserted as `#figure`/`#grid`)

Generated artifacts are written under `.typst_py/` next to the source file.

## Caching

Each block is hashed by its Python code (SHA-256) and its display options (SHA-256) separately.

**Three cache outcomes per block:**

| Situation | Action | Performance |
|---|---|---|
| Code + options unchanged | Use cached output directly | Instant — no kernel started |
| Code unchanged, options changed | Re-render from cached execution data | Instant — no kernel started |
| Code changed | Re-execute from that block onward | Kernel started only when needed |

When all blocks are cached, the kernel is never started — preprocessing is nearly instantaneous.

### Cascade Behavior

Because all cells share a single kernel namespace, when a block's code changes, **all subsequent blocks are automatically re-executed** (the namespace may have changed). Blocks before the changed one remain cached.

**Exception:** `refresh: true` blocks re-execute every run but don't trigger a cascade — they run with the same code, so the namespace output is presumed unchanged.

## Block Options

Place `%|` option lines at the very top of a Python code block.

### Execution Control

| Option | Values | Default | Description |
|---|---|---|---|
| `execute` | `true` / `false` | `true` | Whether to run the code |
| `echo` | `true` / `false` | `true` | Whether to show the source listing |
| `refresh` | `true` / `false` | `false` | Force re-execution every run (no cascade) |

**`refresh: true`** — Re-executes *only this block* on every run, regardless of cache. Downstream blocks are **not** automatically re-executed. Use this for blocks that depend on external state (files, databases, time, randomness).

**Example:**

````typst
```python
%| refresh: true
data = load_from_database()
```
````

### Figure Behavior

| Option | Values | Default | Notes |
|---|---|---|---|
| `caption` | text | none | Explicit block caption |
| `label` | typst label name | none | Outer figure label `<name>` |
| `keep-subplots` | `true` / `false` | `false` | Keep multi-axes figures as one image |

### Typst Parameter Passthrough

All Typst arguments are passed via prefixes only:

| Prefix | Target call | Example |
|---|---|---|
| `img-xxx` | `image(xxx: ...)` | `%| img-width: 80%` |
| `fig-xxx` | `figure(xxx: ...)` | `%| fig-placement: top` |
| `grid-xxx` | `grid(xxx: ...)` | `%| grid-gutter: 1em` |

## Figure And Grid Behavior

- Each axes is saved as a separate image by default.
- `%| keep-subplots: true` keeps the full matplotlib figure as one image.
- Multiple images from one block are emitted as a Typst `grid(...)`.
- Subfigures use `kind: "subfigure"`.
- Child labels use letter suffixes from block label:
  - `%| label: fig-1` → outer `<fig-1>`, children `<fig-1a>`, `<fig-1b>`, ...

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

Cached blocks are reused across re-preprocessing runs, so editing a single block in watch mode is fast even in large documents.

## CLI

```text
usage: typst_ipy.py [-c | -w] [options] input.typ

  -c, --compile        Compile to PDF once
  -w, --watch          Watch for changes, preprocess on save, live preview

  -d, --debug          Keep intermediate generated .typ file after compile
  --images-dir PATH    Image directory inside .typst_py/ (default: img)
  --interval SEC       Polling interval in seconds for watch mode (default: 1.0)
  --python PATH        Path to a Python interpreter for the kernel
                       (e.g. a virtualenv's bin/python)
```

## File Layout

```text
project/
├── report.typ
├── typst_ipy.py
└── .typst_py/
    ├── report.generated.typ
    ├── cache.json
    └── img/
        ├── b1_f1.png
        ├── b2_f1_a1.png
        └── ...
```

Add `.typst_py/` to `.gitignore`.

## License

This project is licensed under the MIT License.
See `LICENSE` for the full text.
