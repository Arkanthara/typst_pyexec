#!/usr/bin/env python3
"""
typst_pyexecutor.py – Execute Python code blocks embedded in a Typst document.

Reads a .typ source file, finds every ```python … ``` fenced block, executes
the code in a shared namespace, and replaces each fence with:
  - source listing      (unless %| echo: false)
  - printed output      (when execute: true)
  - matplotlib figures  (saved as PNG, inserted as #figure/#grid)

Performance
-----------
Namespace snapshots are saved after each block execution (using dill or pickle).
When a block changes, the namespace is restored from the previous block's
snapshot instead of replaying all earlier code. This eliminates the main
bottleneck of the naive approach (replaying every prior block on each change).

Per-block options  (place at the very top of the block)
------------------------------------------------------
  %| execute: false        skip execution
  %| echo: false           hide source listing
  %| refresh: true         force re-execution of this block every run
  %| execute-all: true     re-execute all blocks from block 1 through this one

  %| caption: My figure    block-level caption (overrides auto-detection)
  %| label: fig-name       Typst label for @fig-name cross-references
  %| keep-title: true      keep matplotlib titles on plots
  %| keep-subplots: true   keep plt.subplots() as a single image

  %| img-xxx: <value>      forwarded as  xxx: <value>  inside image()
  %| fig-xxx: <value>      forwarded as  xxx: <value>  inside figure()
  %| grid-xxx: <value>     forwarded as  xxx: <value>  inside grid()

Special option behavior:
  refresh: true     → Re-execute ONLY this block on every run. The state from
                      previous blocks is restored from a snapshot. Downstream
                      blocks are NOT automatically re-executed (add refresh to
                      them too if needed).
  execute-all: true → Re-execute ALL blocks from the first through this one.
                      Downstream blocks are also cascaded for re-execution
                      since the shared namespace may have changed.

Subfigure show rules
--------------------
Subfigures are emitted as figure(kind: "subfigure"). To format them properly,
add these show rules to your Typst template:

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

CLI
---
  uv run typst_pyexecutor.py -c report.typ       compile → report.pdf
  uv run typst_pyexecutor.py -w report.typ       watch + live preview

Dependencies: standard library only.  matplotlib and dill are optional.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pickle
import subprocess
import sys
import textwrap
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Optional: matplotlib for figure capture ───────────────────────────────────

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

# ── Optional: dill for robust namespace serialization ─────────────────────────
# dill can serialize modules, lambdas, closures, etc. that pickle cannot.
# Falls back to pickle when unavailable.

try:
    import dill as _serializer
except ImportError:
    _serializer = pickle


# ══════════════════════════════════════════════════════════════════════════════
#  Section 1 — Parsing
# ══════════════════════════════════════════════════════════════════════════════

# Options parsed as booleans from %| directives
_BOOL_OPTIONS = {
    "execute", "echo", "keep_title", "keep_subplots", "refresh", "execute_all",
}

# Options that control execution flow but don't affect display rendering.
# These are excluded from the options signature so toggling them doesn't
# trigger a spurious "re-render" when only display opts should matter.
_CONTROL_OPTIONS = {"refresh", "execute_all"}


@dataclass
class BlockOptions:
    """Parsed per-block options from %| directives at the top of a code block."""

    # Execution control
    execute: bool = True
    echo: bool = True
    refresh: bool = False
    execute_all: bool = False

    # Figure behavior
    keep_title: bool = False
    keep_subplots: bool = False
    caption: str | None = None
    label: str | None = None

    # Typst parameter passthrough
    img_params: dict[str, str] = field(default_factory=dict)
    fig_params: dict[str, str] = field(default_factory=dict)
    grid_params: dict[str, str] = field(default_factory=dict)

    def display_dict(self) -> dict:
        """Serializable dict of display-affecting options (excludes control opts)."""
        return {
            "execute": self.execute,
            "echo": self.echo,
            "keep_title": self.keep_title,
            "keep_subplots": self.keep_subplots,
            "caption": self.caption,
            "label": self.label,
            "img": self.img_params,
            "fig": self.fig_params,
            "grid": self.grid_params,
        }


def parse_options(lines: list[str]) -> tuple[BlockOptions, str]:
    """Extract %| option directives from the top of a code block.

    Returns (parsed BlockOptions, remaining Python code as a string).
    """
    opts = BlockOptions()
    code_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("%|") and ":" in stripped[2:]:
            key, _, value = stripped[2:].partition(":")
            key = key.strip().lower().replace("-", "_")
            value = value.strip()

            # Route to the appropriate parameter group
            if key.startswith("img_"):
                opts.img_params[key[4:].replace("_", "-")] = value
            elif key.startswith("fig_"):
                opts.fig_params[key[4:].replace("_", "-")] = value
            elif key.startswith("grid_"):
                opts.grid_params[key[5:].replace("_", "-")] = value
            elif key in _BOOL_OPTIONS:
                setattr(opts, key, value.lower() not in ("false", "no", "0", "off"))
            elif hasattr(opts, key):
                setattr(opts, key, None if value.lower() == "none" else value)

            code_start = i + 1
        elif stripped.startswith("%"):
            code_start = i + 1
        else:
            code_start = i
            break
    else:
        code_start = len(lines)

    return opts, textwrap.dedent("".join(lines[code_start:]))


# ── Document structure ────────────────────────────────────────────────────────


@dataclass
class ParsedBlock:
    """A Python code block extracted from the Typst source."""

    block_id: int  # 1-based block number
    opts: BlockOptions
    code: str


@dataclass
class Segment:
    """A piece of the document: either literal text or a reference to a block."""

    kind: str  # "text" or "block"
    payload: Any  # str (text content) for "text", int (block list index) for "block"


def parse_document(source: Path) -> tuple[list[Segment], list[ParsedBlock]]:
    """Parse a Typst source file into text segments and Python code blocks.

    Returns (segments, blocks) where each "block" segment has a payload
    pointing into the blocks list.
    """
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    segments: list[Segment] = []
    blocks: list[ParsedBlock] = []
    block_num = 0
    i = 0

    while i < len(lines):
        # Regular text line
        if lines[i].strip() != "```python":
            segments.append(Segment("text", _rewrite_import_path(lines[i])))
            i += 1
            continue

        # Opening fence found — collect the block body
        block_num += 1
        i += 1  # skip ```python
        raw: list[str] = []
        while i < len(lines) and lines[i].strip() != "```":
            raw.append(lines[i])
            i += 1

        if i >= len(lines):
            raise SyntaxError(f"Unclosed ```python block #{block_num} in {source}")
        i += 1  # skip closing ```

        opts, code = parse_options(raw)
        blocks.append(ParsedBlock(block_id=block_num, opts=opts, code=code))
        segments.append(Segment("block", len(blocks) - 1))

    return segments, blocks


def _rewrite_import_path(line: str) -> str:
    """Prefix relative #import/#include paths with ../ for the generated file."""
    stripped = line.lstrip()
    if not (stripped.startswith("#import") or stripped.startswith("#include")):
        return line

    q1 = line.find('"')
    if q1 == -1:
        return line
    q2 = line.find('"', q1 + 1)
    if q2 == -1:
        return line

    path = line[q1 + 1 : q2]
    if path.startswith(("/", "./", "../", "@")) or "://" in path:
        return line
    return line[: q1 + 1] + "../" + path + line[q2:]


# ══════════════════════════════════════════════════════════════════════════════
#  Section 2 — Typst markup generation
# ══════════════════════════════════════════════════════════════════════════════

_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def _fmt_args(params: dict[str, str]) -> str:
    """Format a dict as comma-separated Typst named arguments."""
    return ", ".join(f"{k}: {v}" for k, v in params.items())


def typst_raw(text: str, lang: str = "") -> str:
    """Wrap text in a Typst ``#raw(…)`` call."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if lang:
        return f'#raw("{escaped}", block: true, lang: "{lang}")\n\n'
    return f'#raw("{escaped}", block: true)\n\n'


def typst_image(path: str, extra: dict[str, str]) -> str:
    """Build an ``image("path", ...)`` call."""
    parts = [f'"{path}"']
    if extra:
        parts.append(_fmt_args(extra))
    return f'image({", ".join(parts)})'


def typst_figure(
    body: str,
    extra: dict[str, str],
    *,
    caption: str | None = None,
    label: str | None = None,
) -> str:
    """Build ``#figure(body, ...) <label>`` markup."""
    params = dict(extra)
    if caption is not None and "caption" not in params:
        params["caption"] = f"[{caption}]"
    parts = [body]
    if params:
        parts.append(_fmt_args(params))
    inner = f'figure({", ".join(parts)})'
    lbl = f" <{label}>" if label else ""
    return f"#{inner}{lbl}\n\n"


def typst_subfigure(
    body: str,
    extra: dict[str, str],
    *,
    caption: str | None = None,
    label: str | None = None,
) -> str:
    """Build a subfigure (``figure`` with ``kind: "subfigure"``) for grid children."""
    params: dict[str, str] = {"kind": '"subfigure"'}
    params.update(extra)
    if caption is not None and "caption" not in params:
        params["caption"] = f"[{caption}]"
    parts = [body]
    if params:
        parts.append(_fmt_args(params))
    inner = f'figure({", ".join(parts)})'
    lbl = f" <{label}>" if label else ""
    return f"[#{inner}{lbl}]"


def typst_grid(children: list[str], extra: dict[str, str], ncols: int) -> str:
    """Build ``grid(columns: N, ...)`` call."""
    params: dict[str, str] = {"columns": str(ncols)}
    params.update(extra)
    parts = [_fmt_args(params)] + children
    return f'grid({", ".join(parts)})'


# ══════════════════════════════════════════════════════════════════════════════
#  Section 3 — Matplotlib capture
# ══════════════════════════════════════════════════════════════════════════════


def _clean_title(title: str) -> str:
    """Strip LaTeX markup from a matplotlib title for use as a Typst caption."""
    if "\\" in title:
        title = title.replace("\\", "").replace("{", "(").replace("}", ")")
    return title


def _get_suptitle(fig: Any) -> str:
    """Extract and clean the figure's suptitle, or return ``""``."""
    if fig._suptitle is not None and fig._suptitle.get_text():
        return _clean_title(fig._suptitle.get_text())
    return ""


def _detect_ncols(fig: Any) -> int | None:
    """Detect the number of grid columns from the figure's GridSpec."""
    axes = fig.get_axes()
    if not axes:
        return None
    try:
        gs = axes[0].get_gridspec()
        if gs and all(ax.get_gridspec() is gs for ax in axes):
            return gs.ncols
    except Exception:
        pass
    return None


def _save_single_axes(fig: Any, ax: Any, path: Path) -> None:
    """Save one axes from a multi-axes figure as a standalone PNG image."""
    visible = [a for a in fig.get_axes() if a.get_visible()]
    idx = visible.index(ax)
    fig_copy = pickle.loads(pickle.dumps(fig))
    for i, other in enumerate(a for a in fig_copy.get_axes() if a.get_visible()):
        if i != idx:
            fig_copy.delaxes(other)
    fig_copy.savefig(path, bbox_inches="tight")
    plt.close(fig_copy)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 4 — Block execution
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ExecResult:
    """Raw output from executing a single code block."""

    text: str = ""
    images: list[dict] = field(default_factory=list)  # [{"path", "caption"}]
    ncols: int | None = None
    block_caption: str | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "images": self.images,
            "ncols": self.ncols,
            "block_caption": self.block_caption,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ExecResult:
        return cls(
            text=d.get("text", ""),
            images=d.get("images", []),
            ncols=d.get("ncols"),
            block_caption=d.get("block_caption"),
        )

    @classmethod
    def empty(cls) -> ExecResult:
        return cls()


def execute_block(
    code: str,
    *,
    ns: dict,
    block_id: int,
    abs_img_dir: Path,
    rel_img_dir: Path,
    opts: BlockOptions,
) -> tuple[str, ExecResult]:
    """Execute Python code, capture stdout/stderr and matplotlib figures.

    The namespace ``ns`` is modified in-place (shared across blocks).
    Returns ``(typst_markup, ExecResult)`` for caching and output assembly.
    """
    buf_out, buf_err = io.StringIO(), io.StringIO()
    open_figs = set(plt.get_fignums()) if plt else set()
    if plt:
        plt.show = lambda *a, **kw: None

    # Run the code
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compile(code, f"<block {block_id}>", "exec"), ns)
    except Exception:
        err_text = traceback.format_exc().strip()
        return typst_raw(err_text, lang="text"), ExecResult(text=err_text)

    # Collect text output
    out = buf_out.getvalue()
    err = buf_err.getvalue()
    text = (out + "\n[stderr]\n" + err) if err else out

    parts: list[str] = []
    if text.strip():
        parts.append(typst_raw(text.strip(), lang="text"))

    if not plt:
        return "".join(parts), ExecResult(text=text)

    # Collect new matplotlib figures
    new_figs = sorted(set(plt.get_fignums()) - open_figs)
    if not new_figs:
        return "".join(parts), ExecResult(text=text)

    abs_img_dir.mkdir(parents=True, exist_ok=True)
    images: list[tuple[str, str | None]] = []
    block_caption: str | None = None
    detected_cols: int | None = None

    for fpos, fnum in enumerate(new_figs, 1):
        fig = plt.figure(fnum)
        vis_axes = [ax for ax in fig.get_axes() if ax.get_visible()]
        should_split = not opts.keep_subplots and len(vis_axes) > 1

        suptitle = _get_suptitle(fig)
        if suptitle and block_caption is None:
            block_caption = suptitle

        if should_split:
            # Split multi-axes figure into individual images
            if detected_cols is None:
                detected_cols = _detect_ncols(fig)
            if fig._suptitle is not None:
                fig.suptitle("")

            for ai, ax in enumerate(vis_axes, 1):
                ax_title = _clean_title(ax.get_title())
                if not opts.keep_title:
                    ax.set_title("")
                fname = f"b{block_id}_f{fpos}_a{ai}.png"
                _save_single_axes(fig, ax, abs_img_dir / fname)
                images.append(((rel_img_dir / fname).as_posix(), ax_title or None))
        else:
            # Save the whole figure as one image
            if len(vis_axes) == 1:
                ax_title = _clean_title(vis_axes[0].get_title())
                if ax_title and block_caption is None:
                    block_caption = ax_title
            if not opts.keep_title:
                if fig._suptitle is not None:
                    fig.suptitle("")
                for ax in vis_axes:
                    ax.set_title("")
            fname = f"b{block_id}_f{fpos}.png"
            fig.savefig(abs_img_dir / fname, bbox_inches="tight")
            images.append(((rel_img_dir / fname).as_posix(), suptitle or None))

        plt.close(fig)

    result = ExecResult(
        text=text,
        images=[{"path": p, "caption": c} for p, c in images],
        ncols=detected_cols,
        block_caption=block_caption,
    )

    if images:
        parts += _build_figure_markup(images, detected_cols, block_caption, opts)

    return "".join(parts), result


# ══════════════════════════════════════════════════════════════════════════════
#  Section 5 — Figure markup builder
# ══════════════════════════════════════════════════════════════════════════════


def _build_figure_markup(
    images: list[tuple[str, str | None]],
    detected_cols: int | None,
    block_caption: str | None,
    opts: BlockOptions,
) -> list[str]:
    """Build Typst figure/grid markup from image paths and display options."""
    cap = opts.caption
    if cap is None and not opts.keep_title and block_caption:
        cap = block_caption
    label = opts.label

    # Single image → simple figure
    if len(images) == 1:
        return [
            typst_figure(
                typst_image(images[0][0], opts.img_params),
                opts.fig_params,
                caption=cap,
                label=label,
            )
        ]

    # Multiple images → grid of subfigures inside an outer figure
    ncols = detected_cols or len(images)
    grid_extra = dict(opts.grid_params)
    if "columns" in grid_extra:
        ncols_str = grid_extra.pop("columns")
        ncols = int(ncols_str) if ncols_str.isdigit() else ncols

    children: list[str] = []
    for i, (path, sub_cap) in enumerate(images):
        letter = _ALPHA[i] if i < 26 else str(i + 1)
        sub_label = f"{label}{letter}" if label else None
        children.append(
            typst_subfigure(
                typst_image(path, opts.img_params),
                opts.fig_params,
                caption=sub_cap,
                label=sub_label,
            )
        )

    body = typst_grid(children, grid_extra, ncols)
    return [typst_figure(body, {"kind": "image"}, caption=cap, label=label)]


def _render_from_cache(result: ExecResult, opts: BlockOptions) -> str:
    """Reconstruct Typst markup from a cached ExecResult and current options."""
    parts: list[str] = []
    if result.text.strip():
        parts.append(typst_raw(result.text.strip(), lang="text"))
    images = [(d["path"], d.get("caption")) for d in result.images]
    if images:
        parts += _build_figure_markup(images, result.ncols, result.block_caption, opts)
    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 6 — Cache management
# ══════════════════════════════════════════════════════════════════════════════


def _code_sig(code: str) -> str:
    """SHA-256 of the Python source code (ignores options)."""
    return hashlib.sha256(code.encode()).hexdigest()


def _opts_sig(opts: BlockOptions) -> str:
    """SHA-256 of display-affecting options only."""
    payload = json.dumps(opts.display_dict(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class BlockCache:
    """Persistent on-disk cache: signatures, rendered markup, and execution data."""

    code_sigs: list[str] = field(default_factory=list)
    opts_sigs: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)
    exec_data: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> BlockCache:
        """Load from JSON file. Returns empty cache on any failure."""
        if not path.exists():
            return cls()
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                code_sigs=d.get("sigs", []),
                opts_sigs=d.get("opts_sigs", []),
                rendered=d.get("rendered", []),
                exec_data=d.get("exec_data", []),
            )
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        """Persist to JSON file."""
        path.write_text(
            json.dumps(
                {
                    "sigs": self.code_sigs,
                    "opts_sigs": self.opts_sigs,
                    "rendered": self.rendered,
                    "exec_data": self.exec_data,
                }
            ),
            encoding="utf-8",
        )

    def valid_for(self, n: int) -> bool:
        """Check that all cache arrays have the expected block count."""
        return all(
            len(lst) == n
            for lst in (self.code_sigs, self.opts_sigs, self.rendered, self.exec_data)
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Section 7 — Namespace snapshots
# ══════════════════════════════════════════════════════════════════════════════


class NamespaceManager:
    """Save and restore execution namespace state between blocks.

    Uses dill (preferred) or pickle to serialize the namespace after each
    block execution. When a later block needs re-execution, the namespace
    is restored from the nearest available snapshot instead of replaying
    all earlier blocks from scratch.

    Falls back gracefully to replay if serialization fails.
    """

    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = snapshot_dir
        self._available = True

    def save(self, ns: dict, index: int) -> None:
        """Save namespace snapshot after block[index] executes."""
        if not self._available:
            return
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / f"ns_{index}.pkl"
        try:
            filtered = {k: v for k, v in ns.items() if k != "__builtins__"}
            path.write_bytes(_serializer.dumps(filtered))
        except Exception:
            # Serialization failed — disable snapshots for this run
            self._available = False

    def load(self, index: int) -> dict | None:
        """Load namespace snapshot saved after block[index]. Returns None on failure."""
        path = self.snapshot_dir / f"ns_{index}.pkl"
        if not path.exists():
            return None
        try:
            restored = _serializer.loads(path.read_bytes())
            ns: dict = {"__name__": "__main__"}
            ns.update(restored)
            return ns
        except Exception:
            return None

    def clear_from(self, index: int) -> None:
        """Remove all snapshots from the given index onward."""
        if not self.snapshot_dir.exists():
            return
        for p in self.snapshot_dir.glob("ns_*.pkl"):
            try:
                idx = int(p.stem.split("_")[1])
                if idx >= index:
                    p.unlink(missing_ok=True)
            except (ValueError, IndexError):
                pass

    def clear_all(self) -> None:
        """Remove all snapshot files."""
        if not self.snapshot_dir.exists():
            return
        for p in self.snapshot_dir.glob("ns_*.pkl"):
            p.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 8 — Preprocessor (main logic)
# ══════════════════════════════════════════════════════════════════════════════

# Action labels for the per-block execution plan
_CACHED = "cached"
_RERENDER = "rerender"
_EXECUTE = "execute"


def preprocess(
    source: Path,
    output: Path,
    img_dir: Path,
    cache_path: Path,
    snapshot_dir: Path,
) -> None:
    """Parse source, execute changed blocks, write generated .typ file.

    Execution strategy:
      1. Compare code and option signatures against the on-disk cache.
      2. Classify each block as cached / rerender / execute.
      3. Handle special options (refresh, execute-all) and cascade.
      4. Use namespace snapshots to skip replaying unchanged early blocks.
    """
    segments, blocks = parse_document(source)
    n = len(blocks)

    if n == 0:
        # No code blocks — just copy text with import path rewrites
        out = [seg.payload for seg in segments if seg.kind == "text"]
        output.write_text("".join(out), encoding="utf-8")
        return

    # Compute current signatures
    code_sigs = [_code_sig(b.code) for b in blocks]
    opts_sigs = [_opts_sig(b.opts) for b in blocks]

    # Load cached state
    cache = BlockCache.load(cache_path)
    ns_mgr = NamespaceManager(snapshot_dir)
    has_valid_cache = cache.valid_for(n)

    # If block count changed, invalidate all snapshots
    if not has_valid_cache:
        ns_mgr.clear_all()

    # ── Step 1: Classify each block ───────────────────────────────────────────
    actions: list[str] = []
    is_refresh_only: list[bool] = []  # True when EXECUTE is due to refresh alone

    for j in range(n):
        b = blocks[j]
        code_match = has_valid_cache and code_sigs[j] == cache.code_sigs[j]
        opts_match = has_valid_cache and opts_sigs[j] == cache.opts_sigs[j]

        if b.opts.refresh:
            actions.append(_EXECUTE)
            is_refresh_only.append(True)
        elif b.opts.execute_all:
            actions.append(_EXECUTE)
            is_refresh_only.append(False)
        elif code_match and opts_match:
            actions.append(_CACHED)
            is_refresh_only.append(False)
        elif code_match:
            actions.append(_RERENDER)
            is_refresh_only.append(False)
        else:
            actions.append(_EXECUTE)
            is_refresh_only.append(False)

    # ── Step 2: execute-all forces all prior blocks to execute ────────────────
    for j in range(n):
        if blocks[j].opts.execute_all:
            for k in range(j):
                if actions[k] != _EXECUTE:
                    actions[k] = _EXECUTE
                    is_refresh_only[k] = False

    # ── Step 3: Cascade execution forward ─────────────────────────────────────
    # When a block's code changed (or execute-all forced it), all subsequent
    # executable blocks must also run because the shared namespace may differ.
    # Exception: refresh-only blocks don't trigger the cascade (they re-execute
    # with the same code, so the namespace output is presumed unchanged).
    first_cascading = None
    for j in range(n):
        if actions[j] == _EXECUTE and not is_refresh_only[j]:
            first_cascading = j
            break

    if first_cascading is not None:
        for j in range(first_cascading + 1, n):
            if actions[j] in (_CACHED, _RERENDER) and blocks[j].opts.execute:
                actions[j] = _EXECUTE
                is_refresh_only[j] = False

    # ── Step 4: Execute the plan ──────────────────────────────────────────────
    _log_plan(actions)

    rendered: list[str] = []
    exec_data_list: list[dict] = []
    ns: dict | None = None  # Lazily initialised on first EXECUTE block

    for j in range(n):
        b = blocks[j]
        action = actions[j]

        # ── Cached: reuse stored output verbatim ──────────────────────────────
        if action == _CACHED:
            rendered.append(cache.rendered[j])
            exec_data_list.append(cache.exec_data[j])
            continue

        # ── Rerender: same code, different display options ────────────────────
        if action == _RERENDER:
            r = ""
            if b.opts.echo:
                r += typst_raw(b.code.rstrip("\n"), lang="python")
            if b.opts.execute:
                cached_result = ExecResult.from_dict(cache.exec_data[j])
                r += _render_from_cache(cached_result, b.opts)
            rendered.append(r)
            exec_data_list.append(cache.exec_data[j])
            continue

        # ── Execute: run the Python code ──────────────────────────────────────
        if ns is None:
            ns = _restore_namespace(j, blocks, ns_mgr)

        r = ""
        if b.opts.echo:
            r += typst_raw(b.code.rstrip("\n"), lang="python")

        if b.opts.execute and b.code.strip():
            markup, result = execute_block(
                b.code,
                ns=ns,
                block_id=b.block_id,
                abs_img_dir=output.parent / img_dir,
                rel_img_dir=img_dir,
                opts=b.opts,
            )
            r += markup
            edata = result.to_dict()
        else:
            edata = ExecResult.empty().to_dict()

        rendered.append(r)
        exec_data_list.append(edata)

        # Save namespace snapshot after this block
        ns_mgr.save(ns, j)

    # ── Save cache, assemble output ───────────────────────────────────────────
    new_cache = BlockCache(
        code_sigs=code_sigs,
        opts_sigs=opts_sigs,
        rendered=rendered,
        exec_data=exec_data_list,
    )
    new_cache.save(cache_path)

    out_parts: list[str] = []
    for seg in segments:
        if seg.kind == "text":
            out_parts.append(seg.payload)
        else:
            out_parts.append(rendered[seg.payload])
    output.write_text("".join(out_parts), encoding="utf-8")


def _log_plan(actions: list[str]) -> None:
    """Print a human-readable summary of the execution plan."""
    counts = {a: actions.count(a) for a in (_CACHED, _RERENDER, _EXECUTE)}

    if counts[_EXECUTE] == 0 and counts[_RERENDER] == 0:
        print("[typst-py] All blocks cached.")
        return

    if counts[_EXECUTE] == 0:
        print(f"[typst-py] Re-rendering {counts[_RERENDER]} block(s) (options changed).")
        return

    parts = []
    if counts[_CACHED]:
        parts.append(f"{counts[_CACHED]} cached")
    if counts[_RERENDER]:
        parts.append(f"{counts[_RERENDER]} re-rendered")
    parts.append(f"{counts[_EXECUTE]} to execute")
    print(f"[typst-py] {', '.join(parts)}.")


def _restore_namespace(
    target: int, blocks: list[ParsedBlock], ns_mgr: NamespaceManager
) -> dict:
    """Restore the namespace needed *before* executing blocks[target].

    Strategy (tries fast path first, degrades gracefully):
      1. Load snapshot from block target-1 (namespace after the prior block).
      2. Scan backward for the nearest available snapshot.
      3. Replay blocks from the snapshot point up to target-1.
      4. If no snapshot exists at all, replay from a fresh namespace.
    """
    ns: dict | None = None
    replay_from = 0

    # Scan backward for the closest usable snapshot
    for probe in range(target - 1, -1, -1):
        ns = ns_mgr.load(probe)
        if ns is not None:
            replay_from = probe + 1
            break

    if ns is None:
        ns = {"__name__": "__main__"}

    if replay_from < target:
        count = target - replay_from
        if count > 0:
            print(f"[typst-py]   Replaying {count} block(s) to rebuild namespace.")

    # Replay blocks [replay_from .. target-1] to rebuild namespace state
    for j in range(replay_from, target):
        b = blocks[j]
        if b.opts.execute and b.code.strip():
            try:
                exec(compile(b.code, f"<block {b.block_id}>", "exec"), ns)
            except Exception:
                pass
            # Save snapshot so future runs can skip this replay
            ns_mgr.save(ns, j)

    return ns


# ══════════════════════════════════════════════════════════════════════════════
#  Section 9 — Watch mode
# ══════════════════════════════════════════════════════════════════════════════


def watch(
    source: Path,
    generated: Path,
    img_dir: Path,
    cache_path: Path,
    snapshot_dir: Path,
    interval: float,
) -> None:
    """Preprocess on file change, launch ``tinymist preview`` for live reload."""
    print("[typst-py] Initial preprocessing…")
    preprocess(source, generated, img_dir, cache_path, snapshot_dir)

    # Start live-preview server
    cmd = ["tinymist", "preview", str(generated), "--root", str(source.parent)]
    proc = subprocess.Popen(cmd)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("`tinymist preview` failed to start.")

    # Build initial file-change snapshot (exclude generated artifacts)
    watch_root = source.parent.resolve()
    ignore_dirs = {generated.parent.resolve()}

    def snap() -> dict[Path, float]:
        mtimes: dict[Path, float] = {}
        for p in watch_root.rglob("*"):
            if not p.is_file():
                continue
            resolved = p.resolve()
            if any(resolved.is_relative_to(d) for d in ignore_dirs):
                continue
            try:
                mtimes[resolved] = p.stat().st_mtime
            except OSError:
                pass
        return mtimes

    last = snap()
    print(f"[typst-py] Watching {watch_root}  (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(interval)
            if proc.poll() is not None:
                raise RuntimeError("`tinymist preview` exited unexpectedly.")
            current = snap()
            if current != last:
                print("[typst-py] Change detected – re-preprocessing…")
                preprocess(source, generated, img_dir, cache_path, snapshot_dir)
                last = snap()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ══════════════════════════════════════════════════════════════════════════════
#  Section 10 — CLI
# ══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="typst_pyexecutor.py",
        description="Execute Python blocks in a Typst document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              uv run typst_pyexecutor.py -c report.typ    compile → report.pdf
              uv run typst_pyexecutor.py -w report.typ    watch + live preview
        """),
    )

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("-c", "--compile", action="store_true", help="Compile to PDF.")
    mode.add_argument(
        "-w", "--watch", action="store_true",
        help="Watch for changes, preprocess on save, live preview.",
    )

    ap.add_argument("input", type=Path, help="Source .typ file.")
    ap.add_argument(
        "-d", "--debug", action="store_true",
        help="Keep intermediate generated .typ file after compile.",
    )
    ap.add_argument(
        "--images-dir", type=Path, default=Path("img"),
        help="Image directory inside .typst_py/ (default: img).",
    )
    ap.add_argument(
        "--interval", type=float, default=1.0,
        help="Polling interval in seconds for watch mode (default: 1.0).",
    )

    args = ap.parse_args()

    # Validate input
    source = args.input.resolve()
    if not source.exists():
        print(f"[typst-py] ERROR: {source} not found.", file=sys.stderr)
        return 1
    if source.suffix.lower() != ".typ":
        print("[typst-py] ERROR: input must be a .typ file.", file=sys.stderr)
        return 1

    # Set up working directory
    work_dir = source.parent / ".typst_py"
    work_dir.mkdir(parents=True, exist_ok=True)
    generated = work_dir / f"{source.stem}.generated.typ"
    cache_file = work_dir / "block-cache.json"
    snapshot_dir = work_dir / "snapshots"
    pdf = source.with_suffix(".pdf")

    try:
        if args.compile:
            preprocess(source, generated, args.images_dir, cache_file, snapshot_dir)
            print(f"[typst-py] Preprocessed → {generated}")

            result = subprocess.run(
                ["typst", "compile", str(generated), str(pdf),
                 "--root", str(source.parent)],
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("typst compile failed.")
            print(f"[typst-py] Compiled → {pdf}")
        else:
            watch(source, generated, args.images_dir, cache_file,
                  snapshot_dir, args.interval)

    except KeyboardInterrupt:
        print("\n[typst-py] Stopped.")
    except Exception as e:
        print(f"[typst-py] ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        if not args.debug and not args.watch:
            generated.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
