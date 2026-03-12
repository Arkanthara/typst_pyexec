#!/usr/bin/env python3
"""
typst_ipy.py – Execute Python code blocks embedded in a Typst document
using a Jupyter (ipykernel) backend.

Reads a .typ source file, finds every ```python … ``` fenced block, maps them
to Jupyter notebook cells, executes them via an ipykernel, and replaces each
fence with:
  - source listing      (unless %| echo: false)
  - printed output      (when execute: true)
  - matplotlib figures  (saved as PNG, inserted as #figure/#grid)

Execution is backed by a real Jupyter kernel (ipykernel), so cells share a
persistent Python namespace across the session. Results are cached in a JSON
file on disk: if no code changed since the last run, outputs are reused
instantly without starting a kernel.

Per-block options  (place at the very top of the block)
------------------------------------------------------
  %| execute: false        skip execution
  %| echo: false           hide source listing
  %| refresh: true         force re-execution of this block every run

  %| caption: My figure    block-level caption (overrides auto-detection)
  %| label: fig-name       Typst label for @fig-name cross-references
  %| keep-subplots: true   keep plt.subplots() as a single image

  %| img-xxx: <value>      forwarded as  xxx: <value>  inside image()
  %| fig-xxx: <value>      forwarded as  xxx: <value>  inside figure()
  %| grid-xxx: <value>     forwarded as  xxx: <value>  inside grid()

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
  uv run typst_ipy.py -c report.typ       compile → report.pdf
  uv run typst_ipy.py -w report.typ       watch + live preview

Dependencies: jupyter_client, ipykernel.
Optional: matplotlib (figure capture).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jupyter_client.manager import KernelManager

# ══════════════════════════════════════════════════════════════════════════════
#  Section 1 — Parsing
# ══════════════════════════════════════════════════════════════════════════════

_BOOL_OPTIONS = {
    "execute", "echo", "keep_subplots", "refresh",
}


@dataclass
class BlockOptions:
    """Parsed per-block options from %| directives at the top of a code block."""

    execute: bool = True
    echo: bool = True
    refresh: bool = False

    keep_subplots: bool = False
    caption: str | None = None
    label: str | None = None

    img_params: dict[str, str] = field(default_factory=dict)
    fig_params: dict[str, str] = field(default_factory=dict)
    grid_params: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize all options (stored in notebook cell metadata)."""
        return {
            "execute": self.execute,
            "echo": self.echo,
            "refresh": self.refresh,
            "keep_subplots": self.keep_subplots,
            "caption": self.caption,
            "label": self.label,
            "img": self.img_params,
            "fig": self.fig_params,
            "grid": self.grid_params,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BlockOptions:
        return cls(
            execute=d.get("execute", True),
            echo=d.get("echo", True),
            refresh=d.get("refresh", False),
            keep_subplots=d.get("keep_subplots", False),
            caption=d.get("caption"),
            label=d.get("label"),
            img_params=d.get("img", {}),
            fig_params=d.get("fig", {}),
            grid_params=d.get("grid", {}),
        )

    def display_dict(self) -> dict:
        """Options that affect display rendering (excludes control-only opts)."""
        return {
            "execute": self.execute,
            "echo": self.echo,
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

    block_id: int       # 1-based
    opts: BlockOptions
    code: str


@dataclass
class Segment:
    """A piece of the document: either literal text or a reference to a block."""

    kind: str       # "text" or "block"
    payload: Any    # str for text, int (index into blocks list) for block


def parse_document(source: Path) -> tuple[list[Segment], list[ParsedBlock]]:
    """Parse a Typst source file into text segments and Python code blocks."""
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    segments: list[Segment] = []
    blocks: list[ParsedBlock] = []
    block_num = 0
    i = 0

    while i < len(lines):
        if lines[i].strip() != "```python":
            segments.append(Segment("text", _rewrite_import_path(lines[i])))
            i += 1
            continue

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
    return ", ".join(f"{k}: {v}" for k, v in params.items())


def typst_raw(text: str, lang: str = "") -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if lang:
        return f'#raw("{escaped}", block: true, lang: "{lang}")\n\n'
    return f'#raw("{escaped}", block: true)\n\n'


def typst_image(path: str, extra: dict[str, str]) -> str:
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
    params: dict[str, str] = {"columns": str(ncols)}
    params.update(extra)
    parts = [_fmt_args(params)] + children
    return f'grid({", ".join(parts)})'


# ══════════════════════════════════════════════════════════════════════════════
#  Section 3 — Jupyter kernel execution
# ══════════════════════════════════════════════════════════════════════════════


# Injected into the kernel on startup. Sets up matplotlib Agg backend
# and defines the figure-saving helper.
_KERNEL_SETUP_CODE = textwrap.dedent("""\
    import json as _json
    import sys as _sys

    # Use Agg backend (non-interactive, figures stay open until explicitly closed)
    try:
        import matplotlib as _mpl
        _mpl.use('Agg')
        import matplotlib.pyplot as _plt
        import pickle as _pkl

        # Suppress plt.show() — we handle figure saving ourselves
        _plt.show = lambda *a, **kw: None

        def _typst_show_figures(block_id, img_dir, keep_subplots=False):
            \"\"\"Save all open figures, optionally splitting subplots.

            Prints JSON metadata to stdout for the host to parse.
            \"\"\"
            import os
            os.makedirs(img_dir, exist_ok=True)
            figs_data = []
            for fnum in _plt.get_fignums():
                fig = _plt.figure(fnum)
                vis_axes = [ax for ax in fig.get_axes() if ax.get_visible()]

                # Extract suptitle
                suptitle = ""
                if fig._suptitle is not None and fig._suptitle.get_text():
                    raw = fig._suptitle.get_text()
                    if "\\\\" in raw:
                        raw = raw.replace("\\\\", "").replace("{", "(").replace("}", ")")
                    suptitle = raw

                # Detect grid columns
                ncols = None
                if vis_axes:
                    try:
                        gs = vis_axes[0].get_gridspec()
                        if gs and all(ax.get_gridspec() is gs for ax in vis_axes):
                            ncols = gs.ncols
                    except Exception:
                        pass

                should_split = not keep_subplots and len(vis_axes) > 1

                if should_split:
                    if fig._suptitle is not None:
                        fig.suptitle("")
                    images = []
                    for ai, ax in enumerate(vis_axes, 1):
                        ax_title = ax.get_title()
                        if "\\\\" in ax_title:
                            ax_title = ax_title.replace("\\\\", "").replace("{", "(").replace("}", ")")
                        ax.set_title("")
                        fig_copy = _pkl.loads(_pkl.dumps(fig))
                        for i, other in enumerate(a for a in fig_copy.get_axes() if a.get_visible()):
                            if i != ai - 1:
                                fig_copy.delaxes(other)
                        fname = f"b{block_id}_f{fnum}_a{ai}.png"
                        fig_copy.savefig(os.path.join(img_dir, fname), bbox_inches="tight")
                        _plt.close(fig_copy)
                        images.append({"path": fname, "caption": ax_title or None})
                    figs_data.append({"suptitle": suptitle, "ncols": ncols, "images": images})
                else:
                    for ax in vis_axes:
                        ax.set_title("")
                    if fig._suptitle is not None:
                        fig.suptitle("")
                    fname = f"b{block_id}_f{fnum}.png"
                    fig.savefig(os.path.join(img_dir, fname), bbox_inches="tight")
                    cap = suptitle
                    if not cap and len(vis_axes) == 1:
                        cap = vis_axes[0].get_title()
                        if cap and "\\\\" in cap:
                            cap = cap.replace("\\\\", "").replace("{", "(").replace("}", ")")
                    figs_data.append({
                        "suptitle": cap or None,
                        "ncols": ncols,
                        "images": [{"path": fname, "caption": cap or None}],
                    })
                _plt.close(fig)
            if figs_data:
                print("__TYPST_FIGURES__" + _json.dumps(figs_data))

    except ImportError:
        def _typst_show_figures(block_id, img_dir, keep_subplots=False):
            pass
""")


def _make_cell_code(code: str, block_id: int, abs_img_dir: str, keep_subplots: bool) -> str:
    """Wrap user code with try/except and append figure-saving hook.

    The user code runs inside a try/except so that:
    - stdout from successful code is captured normally
    - errors are reported via a special marker (not mixed with figure output)
    - the figure hook always runs (saving any figures created before the error)
    """
    ks = "True" if keep_subplots else "False"
    # Indent user code for the try block
    indented = textwrap.indent(code, "    ")
    return (
        f"import traceback as _tb\n"
        f"_typst_error = None\n"
        f"try:\n"
        f"{indented}\n"
        f"except Exception:\n"
        f"    _typst_error = _tb.format_exc()\n"
        f'_typst_show_figures({block_id}, "{abs_img_dir}", keep_subplots={ks})\n'
        f"if _typst_error:\n"
        f'    print("__TYPST_ERROR__" + _typst_error)\n'
    )


class JupyterExecutor:
    """Manages a Jupyter kernel for executing Python cells."""

    def __init__(self, kernel_name: str = "python3", python_path: str | None = None):
        self._km = KernelManager(kernel_name=kernel_name)
        self._kc = None
        self._python_path = python_path

    def start(self, cwd: str | None = None) -> None:
        """Start the kernel and wait for it to be ready."""
        if self._python_path:
            ks = self._km.kernel_spec
            ks.argv = [self._python_path, "-m", "ipykernel_launcher", "-f", "{connection_file}"]

        self._km.start_kernel(cwd=cwd)
        self._kc = self._km.client()
        self._kc.start_channels()
        self._kc.wait_for_ready(timeout=60)

    def setup(self) -> None:
        """Inject helper code into the kernel (matplotlib setup, etc.)."""
        self._execute_silent(_KERNEL_SETUP_CODE)

    def execute(self, code: str) -> tuple[str, str, list[dict]]:
        """Execute code in the kernel. Returns (stdout, stderr, figures_json)."""
        self._kc.execute(code, allow_stdin=False)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        error_text = ""
        figures_json: list[dict] = []

        while True:
            try:
                msg = self._kc.get_iopub_msg(timeout=120)
            except Exception:
                break

            msg_type = msg["msg_type"]
            content = msg.get("content", {})

            if msg_type == "stream":
                text = content.get("text", "")
                for line in text.split("\n"):
                    if line.startswith("__TYPST_FIGURES__"):
                        try:
                            figures_json.extend(json.loads(line[17:]))
                        except json.JSONDecodeError:
                            stdout_parts.append(line + "\n")
                    elif line.startswith("__TYPST_ERROR__"):
                        error_text += line[15:] + "\n"
                    elif content.get("name") == "stderr":
                        stderr_parts.append(line + "\n" if line else "")
                    else:
                        stdout_parts.append(line + "\n" if line else "")

            elif msg_type == "error":
                tb = content.get("traceback", [])
                ansi_re = re.compile(r"\x1b\[[0-9;]*m")
                error_text += "\n".join(ansi_re.sub("", line) for line in tb)

            elif msg_type == "status" and content.get("execution_state") == "idle":
                break

        try:
            self._kc.get_shell_msg(timeout=30)
        except Exception:
            pass

        stdout = "".join(stdout_parts).rstrip("\n")
        stderr = "".join(stderr_parts).rstrip("\n")
        if error_text:
            stderr = (stderr + "\n" + error_text).strip() if stderr else error_text.strip()

        return stdout, stderr, figures_json

    def _execute_silent(self, code: str) -> None:
        """Execute code and discard all output."""
        msg_id = self._kc.execute(code, silent=True, allow_stdin=False)
        while True:
            try:
                msg = self._kc.get_iopub_msg(timeout=30)
                if (msg["msg_type"] == "status"
                        and msg["content"].get("execution_state") == "idle"):
                    break
            except Exception:
                break
        try:
            self._kc.get_shell_msg(timeout=10)
        except Exception:
            pass

    def shutdown(self) -> None:
        """Shut down the kernel cleanly."""
        if self._kc is not None:
            self._kc.stop_channels()
        if self._km.has_kernel:
            self._km.shutdown_kernel(now=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 4 — Figure markup builder
# ══════════════════════════════════════════════════════════════════════════════


def _build_figure_markup(
    images: list[tuple[str, str | None]],
    detected_cols: int | None,
    block_caption: str | None,
    opts: BlockOptions,
) -> list[str]:
    """Build Typst figure/grid markup from image paths and display options."""
    cap = opts.caption or block_caption
    label = opts.label

    if len(images) == 1:
        return [
            typst_figure(
                typst_image(images[0][0], opts.img_params),
                opts.fig_params,
                caption=cap,
                label=label,
            )
        ]

    # Multiple images → grid of subfigures
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


# ══════════════════════════════════════════════════════════════════════════════
#  Section 5 — Cache management
# ══════════════════════════════════════════════════════════════════════════════


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _opts_hash(opts: BlockOptions) -> str:
    payload = json.dumps(opts.display_dict(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class CacheEntry:
    """Cached data for one block."""

    code_hash: str
    opts_hash: str
    rendered: str           # Pre-built Typst markup
    figures: list[dict]     # Figure metadata from _typst_show_figures
    stdout: str
    stderr: str


class NotebookCache:
    """On-disk cache stored as a JSON file alongside the notebook."""

    def __init__(self, path: Path):
        self._path = path
        self.entries: list[CacheEntry] = []

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.entries = [
                CacheEntry(
                    code_hash=e["code_hash"],
                    opts_hash=e["opts_hash"],
                    rendered=e["rendered"],
                    figures=e.get("figures", []),
                    stdout=e.get("stdout", ""),
                    stderr=e.get("stderr", ""),
                )
                for e in data.get("blocks", [])
            ]
        except Exception:
            self.entries = []

    def save(self) -> None:
        data = {
            "blocks": [
                {
                    "code_hash": e.code_hash,
                    "opts_hash": e.opts_hash,
                    "rendered": e.rendered,
                    "figures": e.figures,
                    "stdout": e.stdout,
                    "stderr": e.stderr,
                }
                for e in self.entries
            ]
        }
        self._path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, index: int) -> CacheEntry | None:
        if index < len(self.entries):
            return self.entries[index]
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Section 6 — Preprocessor (main logic)
# ══════════════════════════════════════════════════════════════════════════════

# Action labels
_CACHED = "cached"
_RERENDER = "rerender"
_EXECUTE = "execute"
_SKIP = "skip"


def preprocess(
    source: Path,
    output: Path,
    img_dir: Path,
    cache_path: Path,
    *,
    python_path: str | None = None,
) -> None:
    """Parse source, execute changed blocks via Jupyter kernel, write .typ."""
    segments, blocks = parse_document(source)
    n = len(blocks)

    if n == 0:
        out = [seg.payload for seg in segments if seg.kind == "text"]
        output.write_text("".join(out), encoding="utf-8")
        return

    # Load cache
    cache = NotebookCache(cache_path)
    cache.load()

    # Compute current hashes
    code_hashes = [_code_hash(b.code) for b in blocks]
    opts_hashes = [_opts_hash(b.opts) for b in blocks]

    # ── Classify each block ───────────────────────────────────────────────────
    actions: list[str] = []
    has_matching_cache = len(cache.entries) == n

    for j in range(n):
        b = blocks[j]
        cached = cache.get(j) if has_matching_cache else None

        if not b.opts.execute:
            actions.append(_SKIP)
        elif b.opts.refresh:
            actions.append(_EXECUTE)
        elif cached and cached.code_hash == code_hashes[j]:
            if cached.opts_hash == opts_hashes[j]:
                actions.append(_CACHED)
            else:
                actions.append(_RERENDER)
        else:
            actions.append(_EXECUTE)

    # ── Cascade: if a block's code changed, all subsequent must re-execute ────
    # (because the kernel namespace is sequential)
    # Exception: refresh-only blocks don't cascade (same code, same effect)
    first_cascading = None
    for j in range(n):
        if actions[j] == _EXECUTE and not blocks[j].opts.refresh:
            first_cascading = j
            break

    if first_cascading is not None:
        for j in range(first_cascading + 1, n):
            if actions[j] in (_CACHED, _RERENDER):
                actions[j] = _EXECUTE

    # ── Log plan ──────────────────────────────────────────────────────────────
    needs_kernel = _EXECUTE in actions
    _log_plan(actions, n)

    # ── Start kernel only if needed ───────────────────────────────────────────
    executor: JupyterExecutor | None = None
    abs_img_dir = output.parent / img_dir

    if needs_kernel:
        _log("Starting Jupyter kernel…")
        executor = JupyterExecutor(python_path=python_path)
        executor.start(cwd=str(source.parent))
        executor.setup()
        _log("Kernel ready.")

    # ── Execute the plan ──────────────────────────────────────────────────────
    new_entries: list[CacheEntry] = []

    for j in range(n):
        b = blocks[j]
        action = actions[j]
        progress = f"[{j + 1}/{n}]"

        if action == _SKIP:
            _log(f"{progress} Block {b.block_id}: skipped (execute: false)")
            markup = ""
            if b.opts.echo:
                markup = typst_raw(b.code.rstrip("\n"), lang="python")
            new_entries.append(CacheEntry(
                code_hash=code_hashes[j],
                opts_hash=opts_hashes[j],
                rendered=markup,
                figures=[],
                stdout="",
                stderr="",
            ))
            continue

        if action == _CACHED:
            _log(f"{progress} Block {b.block_id}: cached ✓")
            new_entries.append(cache.entries[j])
            continue

        if action == _RERENDER:
            _log(f"{progress} Block {b.block_id}: re-rendering (options changed)")
            cached = cache.entries[j]
            markup = _render_block(b, cached.stdout, cached.stderr, cached.figures, img_dir)
            new_entries.append(CacheEntry(
                code_hash=code_hashes[j],
                opts_hash=opts_hashes[j],
                rendered=markup,
                figures=cached.figures,
                stdout=cached.stdout,
                stderr=cached.stderr,
            ))
            continue

        # EXECUTE
        _log(f"{progress} Block {b.block_id}: executing…")
        abs_img_dir.mkdir(parents=True, exist_ok=True)

        cell_code = _make_cell_code(
            b.code, b.block_id,
            str(abs_img_dir),
            b.opts.keep_subplots,
        )
        stdout, stderr, figures = executor.execute(cell_code)

        markup = _render_block(b, stdout, stderr, figures, img_dir)
        new_entries.append(CacheEntry(
            code_hash=code_hashes[j],
            opts_hash=opts_hashes[j],
            rendered=markup,
            figures=figures,
            stdout=stdout,
            stderr=stderr,
        ))

    # ── Shutdown kernel ───────────────────────────────────────────────────────
    if executor is not None:
        executor.shutdown()
        _log("Kernel shut down.")

    # ── Save cache ────────────────────────────────────────────────────────────
    cache.entries = new_entries
    cache.save()

    # ── Assemble output ───────────────────────────────────────────────────────
    out_parts: list[str] = []
    for seg in segments:
        if seg.kind == "text":
            out_parts.append(seg.payload)
        else:
            out_parts.append(new_entries[seg.payload].rendered)
    output.write_text("".join(out_parts), encoding="utf-8")
    _log("Output written.")


def _render_block(
    block: ParsedBlock,
    stdout: str,
    stderr: str,
    figures: list[dict],
    rel_img_dir: Path,
) -> str:
    """Build Typst markup for a block from its execution output."""
    parts: list[str] = []
    opts = block.opts

    if opts.echo:
        parts.append(typst_raw(block.code.rstrip("\n"), lang="python"))

    if not opts.execute:
        return "".join(parts)

    # Text output
    text = stdout
    if stderr:
        text = (text + "\n[stderr]\n" + stderr).strip() if text else stderr
    if text.strip():
        parts.append(typst_raw(text.strip(), lang="text"))

    # Figure output
    if figures:
        all_images: list[tuple[str, str | None]] = []
        block_caption: str | None = None
        detected_cols: int | None = None

        for fig_data in figures:
            suptitle = fig_data.get("suptitle")
            ncols = fig_data.get("ncols")
            if suptitle and block_caption is None:
                block_caption = suptitle
            if ncols and detected_cols is None:
                detected_cols = ncols

            for img in fig_data.get("images", []):
                img_path = (rel_img_dir / img["path"]).as_posix()
                all_images.append((img_path, img.get("caption")))

        if all_images:
            parts += _build_figure_markup(all_images, detected_cols, block_caption, opts)

    return "".join(parts)


def _log_plan(actions: list[str], total: int) -> None:
    counts = {a: actions.count(a) for a in (_CACHED, _RERENDER, _EXECUTE, _SKIP)}

    if counts[_EXECUTE] == 0 and counts[_RERENDER] == 0:
        _log(f"All {total} blocks cached — no kernel needed.")
        return

    parts = []
    if counts[_CACHED]:
        parts.append(f"{counts[_CACHED]} cached")
    if counts[_RERENDER]:
        parts.append(f"{counts[_RERENDER]} to re-render")
    if counts[_EXECUTE]:
        parts.append(f"{counts[_EXECUTE]} to execute")
    if counts[_SKIP]:
        parts.append(f"{counts[_SKIP]} skipped")
    _log(f"Plan: {', '.join(parts)}.")


def _log(msg: str) -> None:
    print(f"[typst-ipy] {msg}")


# ══════════════════════════════════════════════════════════════════════════════
#  Section 7 — Watch mode
# ══════════════════════════════════════════════════════════════════════════════


def watch(
    source: Path,
    generated: Path,
    img_dir: Path,
    cache_path: Path,
    interval: float,
    *,
    python_path: str | None = None,
) -> None:
    """Preprocess on file change, launch tinymist preview for live reload."""
    _log("Initial preprocessing…")
    preprocess(source, generated, img_dir, cache_path, python_path=python_path)

    cmd = ["tinymist", "preview", str(generated), "--root", str(source.parent)]
    proc = subprocess.Popen(cmd)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("`tinymist preview` failed to start.")

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
    _log(f"Watching {watch_root}  (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(interval)
            if proc.poll() is not None:
                raise RuntimeError("`tinymist preview` exited unexpectedly.")
            current = snap()
            if current != last:
                _log("Change detected – re-preprocessing…")
                preprocess(source, generated, img_dir, cache_path, python_path=python_path)
                last = snap()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ══════════════════════════════════════════════════════════════════════════════
#  Section 8 — CLI
# ══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="typst_ipy.py",
        description="Execute Python blocks in a Typst document via Jupyter kernel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              uv run typst_ipy.py -c report.typ              compile → report.pdf
              uv run typst_ipy.py -w report.typ              watch + live preview
              uv run typst_ipy.py -c report.typ --python /path/to/venv/bin/python
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
    ap.add_argument(
        "--python", type=str, default=None,
        help="Path to a Python interpreter (e.g. a virtualenv) for the kernel.",
    )

    args = ap.parse_args()

    source = args.input.resolve()
    if not source.exists():
        print(f"[typst-ipy] ERROR: {source} not found.", file=sys.stderr)
        return 1
    if source.suffix.lower() != ".typ":
        print("[typst-ipy] ERROR: input must be a .typ file.", file=sys.stderr)
        return 1

    work_dir = source.parent / ".typst_py"
    work_dir.mkdir(parents=True, exist_ok=True)
    generated = work_dir / f"{source.stem}.generated.typ"
    cache_file = work_dir / "cache.json"
    pdf = source.with_suffix(".pdf")

    try:
        if args.compile:
            preprocess(
                source, generated, args.images_dir, cache_file,
                python_path=args.python,
            )
            _log(f"Preprocessed → {generated}")

            result = subprocess.run(
                ["typst", "compile", str(generated), str(pdf),
                 "--root", str(source.parent)],
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("typst compile failed.")
            _log(f"Compiled → {pdf}")
        else:
            watch(
                source, generated, args.images_dir, cache_file,
                args.interval, python_path=args.python,
            )

    except KeyboardInterrupt:
        print("\n[typst-ipy] Stopped.")
    except Exception as e:
        print(f"[typst-ipy] ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        if not args.debug and not args.watch:
            generated.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
