# typst-pyexec

A single-file Python preprocessor that executes fenced `python` blocks embedded in [Typst](https://typst.app) documents and replaces them with rendered output — source code listing, printed text, and matplotlib figures.

## Requirements

| Tool | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Run the script with dependencies |
| [typst](https://github.com/typst/typst) | Compile mode (`-c`) |
| [tinymist](https://github.com/Myriad-Dreamin/tinymist) | Preview mode (`-p`) |
| matplotlib | Optional — only needed for figure output |

## Quickstart

```powershell
# Compile to PDF
uv run --with matplotlib typst_pyexec.py -c report.typ

# Live browser preview (Python blocks re-execute on save)
uv run --with matplotlib typst_pyexec.py -p report.typ
```

Add any extra packages your code needs with additional `--with` flags:

```powershell
uv run --with numpy --with pandas --with matplotlib typst_pyexec.py -c report.typ
```

## How it works

1. The script reads your `.typ` source file and finds every ` ```python … ``` ` fenced block.
2. All blocks are executed in a single shared Python namespace (imports and variables from earlier blocks are available in later ones).
3. Each block is replaced in the output with:
   - **Source listing** — shown as a styled Typst code block (unless `%| echo: false`)
   - **Printed output** — shown as a raw text block (when `execute: true`)
   - **Matplotlib figures** — saved as PNG and inserted as `#figure(image(…))`

The intermediate `.typ` file and images are written to `.typst_pyexec/` next to your source file.

### Caching

Block outputs are cached by a SHA-256 hash of their code and options. On re-run, only the first changed block and all subsequent blocks are re-executed. Blocks before the first change are replayed silently to restore the shared namespace.

### Preview mode

`-p` launches `tinymist preview` for live browser preview. The source directory is polled every second (configurable with `--interval`). Python blocks re-execute only when you save — not on every keystroke.

---

## Writing blocks

Place `%|` option lines at the very top of the block, before any code:

````typst
```python
%| caption: Portfolio performance
%| columns: 2
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 10, 100)
plt.figure()
plt.plot(x, np.sin(x))
plt.show()
```
````

---

## Block options reference

### Execution control

| Option | Values | Default |
|---|---|---|
| `execute` | `true` / `false` | `true` |
| `echo` | `true` / `false` | `true` |

### Image shorthands — [`image()`](https://typst.app/docs/reference/visualize/image/)

| Option | Example | Default |
|---|---|---|
| `width` | `80%`, `300pt`, `auto` | `auto` |
| `height` | `200pt`, `50%`, `auto` | `auto` |
| `fit` | `cover`, `contain`, `stretch` | `cover` |
| `format` | `png`, `jpg`, `gif`, `svg` | auto-detect |
| `alt` | `My chart` | none |

### Figure shorthands — [`figure()`](https://typst.app/docs/reference/model/figure/)

| Option | Example | Default | Notes |
|---|---|---|---|
| `caption` | `Portfolio value` | auto from `plt.title()` / `plt.suptitle()` | Explicit caption overrides auto-detection |
| `keep-title` | `true` | `false` | Keep matplotlib titles on plots; disable auto-caption |
| `label` | `fig-perf` | none | Attach `<fig-perf>` to the figure; cite with `@fig-perf` |

### Grid shorthands — [`grid()`](https://typst.app/docs/reference/layout/grid/)

| Option | Example | Default | Notes |
|---|---|---|---|
| `grid` | `false` | `true` | Disable grid; emit one `#figure` per image |
| `columns` | `2`, `(1fr, 2fr)` | detected from subplot layout, else image count | Any Typst value |
| `keep-subplots` | `true` | `false` | Keep `plt.subplots()` as one whole image instead of splitting axes |

### Full parameter passthrough

Any Typst parameter can be forwarded using an `img-`, `fig-`, or `grid-` prefix. The value is passed through as a raw Typst expression. Prefix keys override the equivalent shorthand.

| Prefix | Target call | Example |
|---|---|---|
| `img-xxx` | `image(xxx: …)` | `%\| img-width: 80%` |
| `fig-xxx` | `figure(xxx: …)` | `%\| fig-placement: top` |
| `grid-xxx` | `grid(xxx: …)` | `%\| grid-row-gutter: 2em` |

---

## Matplotlib figure behaviour

### Single figure

`plt.title("My title")` is automatically captured as the Typst caption and cleared from the image (unless `keep-title: true`).

```python
plt.figure()
plt.title("Quarterly revenue")
plt.plot(...)
plt.show()
```

Produces: `#figure(image("…"), caption: [Quarterly revenue])`

### Multiple subplots — `plt.subplots()`

By default each axes is saved as a separate PNG. The axes title becomes the caption of its individual `figure()`. Without a block-level suptitle/caption, a bare `#grid(…)` is emitted so each sub-figure gets its own Typst number. With a suptitle or `%| caption:`, the grid is wrapped in a single outer `#figure(grid(…), caption: […])`.

```python
%| caption: Seasonal comparison
fig, axes = plt.subplots(1, 3)
axes[0].set_title("Spring"); ...
axes[1].set_title("Summer"); ...
axes[2].set_title("Autumn"); ...
plt.suptitle("Seasonal comparison")
plt.show()
```

Set `%| keep-subplots: true` to save the whole figure as one image instead.

### Default grid spacing

A `row-gutter: 1em` is added automatically between rows so child captions don't overlap. Override with `%| grid-row-gutter: 2em`.

### Labels and cross-references

```python
%| caption: My figure
%| label: fig-example
plt.plot(...)
plt.show()
```

See @fig-example for details.


For grid mode the outer figure gets `<fig-example>` and each child gets `<fig-example-1>`, `<fig-example-2>`, etc.

---

## CLI reference

```
usage: typst_pyexec.py [-c | -p] [options] input.typ

  -c, --compile        Compile to PDF once
  -p, --preview        Live browser preview via tinymist

  -d, --debug          Keep the intermediate .typ file after compilation
  --images-dir PATH    Image output directory inside .typst_pyexec/ (default: img)
  --interval SEC       Source polling interval for preview mode (default: 1.0)
  --host HOST          Host for tinymist preview
  --port PORT          Port for tinymist preview
```

## File layout

```
project/
├── report.typ                    your source file
├── typst_pyexec.py
└── .typst_pyexec/
    ├── report.generated.typ      intermediate file passed to typst/tinymist
    ├── block-cache.json          block signature cache
    └── img/
        ├── block_001_fig_01.png
        └── ...
```

Add `.typst_pyexec/` to your `.gitignore`.
