# Typst Python Execution Plugin

This project now includes a local plugin runner at:

- `report/typst_pyexec.py`

It preprocesses a Typst file and executes fenced Python blocks delimited by:

```python
# code
```

## Block options

Inside a Python block, put options at the top with `%|`:

```python
%| caption: My multi-panel figure
%| grid-row-gutter: 1em
%| fig-placement: top
import matplotlib.pyplot as plt
fig, axes = plt.subplots(1, 3)
```

### Execution control

| Option | Values | Default |
|---|---|---|
| `execute` | `true` / `false` | `true` |
| `echo` | `true` / `false` | `true` |

### Image shorthands ([`image()`](https://typst.app/docs/reference/visualize/image/))

| Option | Example | Default |
|---|---|---|
| `width` | `80%`, `300pt`, `auto` | `auto` |
| `height` | `200pt`, `50%`, `auto` | `auto` |
| `fit` | `cover`, `contain`, `stretch` | `cover` |
| `format` | `png`, `jpg`, `gif`, `svg` | auto |
| `alt` | `My chart` | none |

### Figure shorthands ([`figure()`](https://typst.app/docs/reference/model/figure/))

| Option | Example | Default | Notes |
|---|---|---|---|
| `caption` | `Portfolio value` | auto from suptitle | |
| `keep-title` | `true` | `false` | Keep all titles on plots; disable auto-caption |

### Grid shorthands ([`grid()`](https://typst.app/docs/reference/layout/grid/))

| Option | Example | Default | Notes |
|---|---|---|---|
| `grid` | `false` | `true` | Disable grid; emit one `#figure` per image |
| `columns` | `2`, `(1fr, 2fr)` | detected from subplot layout | Any Typst value |
| `keep-subplots` | `true` | `false` | Keep `plt.subplots()` as one whole image |

### Full parameter passthrough

Any Typst parameter can be forwarded using a `img-`, `fig-`, or `grid-` prefix.
The value is passed through as a raw Typst expression.

| Prefix | Target | Example |
|---|---|---|
| `img-xxx` | `image(xxx: …)` | `%\| img-width: 80%` |
| `fig-xxx` | `figure(xxx: …)` | `%\| fig-placement: top` |
| `grid-xxx` | `grid(xxx: …)` | `%\| grid-row-gutter: 1em` |

Prefix keys override the equivalent shorthand when both are present.

## Behavior

- One shared Python namespace is used for all executed blocks in source order.
- `print(...)` output is rendered as a raw text block in Typst.
- New matplotlib figures created in a block are saved as PNG files and injected as Typst figures.

## Commands

Run from the folder containing `typst_pyexec.py` and your `.typ` file.

**Live browser preview** (tinymist):

```powershell
uv run typst_pyexec.py -p report.typ
```

`tinymist preview` serves the document locally and auto-refreshes the browser on every save.
Python code blocks are re-executed only on save.

**Compile once to PDF**:

```powershell
uv run typst_pyexec.py -c report.typ
```

Use `-d` to keep the intermediate generated `.typ` file:

```powershell
uv run typst_pyexec.py -c -d report.typ
```

**Optional flags**:

- `--host 127.0.0.1 --port 23625` — bind tinymist preview to a specific address/port
- `--images-dir img` — output directory for generated figures (default: `img`, relative to `.typst_pyexec/`)
- `--interval 1.0` — source-polling interval in seconds for preview mode (default: 1.0)
