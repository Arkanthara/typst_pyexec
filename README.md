# typst-py

A single-file Python preprocessor that executes fenced `python` blocks in [Typst](https://typst.app) documents and replaces them with rendered output.

## Requirements

| Tool | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Run the script |
| [typst](https://github.com/typst/typst) | Compile mode (`-c`) and watch mode (`-w`) |
| matplotlib (optional) | Figure capture from Python blocks |

`typst_py.py` itself uses the Python standard library only. If matplotlib is unavailable, code execution still works but figure capture is disabled.

## Quickstart

```powershell
# Compile once
uv run --with matplotlib typst_py.py -c report.typ

# Watch mode (preprocess on save + typst watch)
uv run --with matplotlib typst_py.py -w report.typ
```

Add extra Python packages your code blocks need with additional `--with` flags:

```powershell
uv run --with numpy --with pandas --with matplotlib typst_py.py -c report.typ
```

## How It Works

1. Read the `.typ` file and find each ` ```python ... ``` ` fenced block.
2. Execute all blocks in one shared namespace.
3. Replace each block with:
   - source listing (unless `%| echo: false`)
   - printed output (when `execute: true`)
   - figures (when matplotlib is installed)

Generated artifacts are written under `.typst_py/` next to the source file.

## Caching And Re-Execution

Each block is hashed from `(options + code)` with SHA-256.

- If all signatures match cache: no Python re-execution.
- Otherwise: only blocks from the first changed block onward are re-run.
- Earlier unchanged blocks are replayed silently to rebuild namespace state.

## Block Options

Place `%|` option lines at the top of a Python block.

### Execution

| Option | Values | Default |
|---|---|---|
| `execute` | `true` / `false` | `true` |
| `echo` | `true` / `false` | `true` |

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

No default `image/figure/grid` options are injected unless required by behavior below.

## Figure And Grid Behavior

- Each axes is saved as a separate image by default.
- `%| keep-subplots: true` keeps the full matplotlib figure as one image.
- Multiple images from one block are always emitted as a Typst `grid(...)`.
- Subfigures default to `kind: "subfigure"`, `supplement: none`, `numbering: "(a)"`.
- Child labels use letter suffixes from block label:
  - `%| label: fig-1` -> outer `<fig-1>`, children `<fig-1a>`, `<fig-1b>`, ...

### Title Conversion

Matplotlib titles are converted for Typst captions by removing backslashes and replacing `{}` with `()`.

## Watch Mode

`-w` does:

1. preprocess once,
2. start `typst watch` on the generated `.typ` file,
3. poll source tree mtimes,
4. re-preprocess only when files are saved.

## CLI

```text
usage: typst_py.py [-c | -w] [options] input.typ

  -c, --compile        Compile to PDF once
  -w, --watch          Preprocess on save + typst watch

  -d, --debug          Keep intermediate .typ file
  --images-dir PATH    Image directory inside .typst_py/ (default: img)
  --interval SEC       Polling interval in seconds (default: 1.0)
```

## File Layout

```text
project/
|- report.typ
|- typst_py.py
`- .typst_py/
   |- report.generated.typ
   |- block-cache.json
   `- img/
      |- b1_f1.png
      `- ...
```

Add `.typst_py/` to `.gitignore`.

## License

This project is licensed under the MIT License.
See `LICENSE` for the full text.
