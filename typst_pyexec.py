#!/usr/bin/env python3
"""
typst_pyexec.py – Execute Python code blocks embedded in a Typst document.

How it works
------------
The script reads a .typ source file, finds every ```python … ``` fenced block,
executes the code, and replaces the fence with:
  - the source shown as a styled code block  (unless %| echo: false)
  - any printed output shown as a text block  (when execute is true)
  - any matplotlib figures saved and inserted as #figure(image(…))  (idem)

All blocks share one Python namespace, so imports and variables defined in an
earlier block are available in later ones.

Per-block options  (place these lines at the very top of the block)
-----------------
  Execution
    %| execute: false     skip execution – only show the source
    %| echo:    false     hide the source – only show output

  Image  – shorthands (typst.app/docs/reference/visualize/image/)
    %| width:   80%       Typst length / percentage / auto               (default: auto)
    %| height:  200pt     Typst length / percentage / auto               (default: auto)
    %| fit:     contain   cover | contain | stretch                      (default: cover)
    %| format:  png       png | jpg | gif | svg                          (default: auto)
    %| alt:     My chart  alternative text string                        (default: none)

  Figure – shorthands (typst.app/docs/reference/model/figure/)
    %| caption: My figure  explicit caption (also used as suptitle capture)
    %| keep-title: true    keep plt titles on plots; disable auto-caption (default: false)

  Grid – shorthands (typst.app/docs/reference/layout/grid/)
    %| grid:    false      emit one #figure per image instead of a grid  (default: true)
    %| columns: 2          columns – plain int or Typst value like (1fr, 2fr)
                           (default: detected from subplot layout, else image count)
    %| keep-subplots: true keep plt.subplots() as one whole image        (default: false)

  Full parameter passthrough (any Typst parameter, raw value)
    %| img-xxx:  <value>   forwarded as  xxx: <value>  inside image()
    %| fig-xxx:  <value>   forwarded as  xxx: <value>  inside figure()
    %| grid-xxx: <value>   forwarded as  xxx: <value>  inside grid()

    Examples:
      %| img-width: 80%
      %| fig-placement: top
      %| grid-row-gutter: 1em

    Prefix keys override the shorthands when the same parameter name is given.

CLI
---
    uv run typst_pyexec.py -c report.typ       compile report.typ → report.pdf
    uv run typst_pyexec.py -p report.typ       live browser preview via tinymist
    uv run typst_pyexec.py -c -d report.typ    compile and keep intermediate .typ

Dependencies
------------
  Standard library only.  matplotlib is optional: figures are captured only
  when it is importable.
"""

import argparse
import hashlib
import io
import json
import subprocess
import sys
import textwrap
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# matplotlib is entirely optional.  When missing, figure capture is disabled.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None


# ── Parsing ──────────────────────────────────────────────────────────────────

# Keys whose value is a True/False flag.
_BOOL_OPTIONS = {"execute", "echo", "keep_title", "grid", "keep_subplots"}


def parse_options(block_lines: list[str]) -> tuple[dict, str]:
    """
    Scan the lines at the top of a ```python block for ``%|`` options.

    Key names are lowercased and hyphens are normalised to underscores, so
    ``keep-title`` and ``keep_title`` are equivalent.

    Returns:
        options  – dict with all supported keys (see module docstring)
        code     – the actual code string after stripping option/comment lines
    """
    options: dict = {
        # execution control
        "execute": True,  "echo": True,
        # figure behaviour
        "keep_title": False,    # True → keep matplotlib title on the plot
        "keep_subplots": False, # True → keep multi-axes figure as whole image
        "grid": True,           # True → wrap all images of a block in one grid
        "columns": None,        # grid columns shorthand; None = auto-detect
        # image() shorthands
        "width": None, "height": None,
        "alt": None, "fit": None, "format": None,
        # figure() shorthand
        "caption": None,
        "label": None,          # Typst label <name> for @name citations
        # prefix-based passthrough dicts (img-*, fig-*, grid-*)
        "_img_extra": {}, "_fig_extra": {}, "_grid_extra": {},
    }
    code_start = 0

    for i, line in enumerate(block_lines):
        stripped = line.strip()

        if stripped.startswith("%|"):
            payload = stripped[2:].strip()
            if ":" in payload:
                key, _, raw_value = payload.partition(":")
                # Normalise: lowercase and replace hyphens with underscores.
                key       = key.strip().lower().replace("-", "_")
                raw_value = raw_value.strip()
                # Route prefix-based passthrough keys first.
                if key.startswith("img_"):
                    options["_img_extra"][key[4:].replace("_", "-")] = raw_value
                elif key.startswith("fig_"):
                    options["_fig_extra"][key[4:].replace("_", "-")] = raw_value
                elif key.startswith("grid_"):
                    options["_grid_extra"][key[5:].replace("_", "-")] = raw_value
                elif key in _BOOL_OPTIONS:
                    options[key] = raw_value.lower() not in ("false", "no", "0", "off")
                elif key in options:
                    options[key] = None if raw_value.lower() == "none" else raw_value
            code_start = i + 1

        elif stripped.startswith("%"):
            # Bare "%" comment line – skip silently.
            code_start = i + 1

        else:
            # First real code line: stop scanning for options.
            code_start = i
            break

    else:
        # Every line was an option/comment – no code at all.
        code_start = len(block_lines)

    code = textwrap.dedent("".join(block_lines[code_start:]))
    return options, code


# ── Output helpers ────────────────────────────────────────────────────────────

def as_raw_block(text: str, lang: str = "") -> str:
    """Wrap plain text in a Typst ``#raw(…)`` call, ready to paste into a .typ file."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if lang:
        return f'#raw("{escaped}", block: true, lang: "{lang}")\n\n'
    return f'#raw("{escaped}", block: true)\n\n'


def _is_relative_typst_path(path_text: str) -> bool:
    """Return True when a Typst path needs to be remapped for generated files."""
    return not (
        path_text.startswith("/")
        or path_text.startswith("./")
        or path_text.startswith("../")
        or path_text.startswith("@")
        or "://" in path_text
    )


def rewrite_relative_paths(line: str) -> str:
    """
    Remap relative paths in ``#import``/``#include`` lines.

    Generated files live one directory deeper than the source (.typst_pyexec/),
    so bare relative paths (e.g. ``"template.typ"``) must become ``"../template.typ"``.
    """
    stripped = line.lstrip()
    if not (stripped.startswith("#import") or stripped.startswith("#include")):
        return line

    first_quote  = line.find('"')
    if first_quote == -1:
        return line
    second_quote = line.find('"', first_quote + 1)
    if second_quote == -1:
        return line

    path_text = line[first_quote + 1 : second_quote]
    if not _is_relative_typst_path(path_text):
        return line

    return line[: first_quote + 1] + "../" + path_text + line[second_quote:]


def _image_call(rel_path: str, opts: dict) -> str:
    """Build just the ``image(…)`` Typst call for a single saved PNG."""
    img_args = [f'"{rel_path}"']
    # Raw Typst expressions: lengths, percentages, "auto" – no extra quoting.
    for key in ("width", "height"):
        if opts.get(key) is not None:
            img_args.append(f"{key}: {opts[key]}")
    # String parameters – must be quoted in Typst.
    for key in ("alt", "fit", "format"):
        if opts.get(key) is not None:
            img_args.append(f'{key}: "{opts[key]}"')
    return f'image({", ".join(img_args)})'


def _figure_markup(
    body: str, caption: str | None, extra: dict | None = None,
    *, code_mode: bool = False, label: str | None = None
) -> str:
    """Wrap *body* (image or grid call) in a Typst ``#figure(…)``.

    *extra* is the ``_fig_extra`` passthrough dict; keys are raw Typst names
    and values are raw Typst expressions.  If *extra* contains ``caption`` it
    overrides the *caption* argument.

    *code_mode*: when ``True`` the figure is a child of a code expression
    (e.g. inside ``grid(…)``).  The result is wrapped in ``[#figure(…)]``
    (a markup content block) so that ``#`` is valid and ``<label>`` works.

    *label*: if given, a Typst label ``<label>`` is attached so the figure
    can be cited as ``@label`` in the document.
    """
    named: dict[str, str] = {}
    # Shorthand caption formatted as a Typst content block.
    if caption is not None:
        named["caption"] = f"caption: [{caption}]"
    # Prefix-based extras (raw) – override shorthand if same key.
    for key, val in (extra or {}).items():
        named[key] = f"{key}: {val}"
    fig_args = [body] + list(named.values())
    inner = f'figure({", ".join(fig_args)})'
    label_suffix = f" <{label}>" if label else ""
    if code_mode:
        return f"[#{inner}{label_suffix}]"
    return f"#{inner}{label_suffix}\n\n"


def _extract_figure_title(fig) -> str:
    """Return the suptitle of *fig*, or empty string if none."""
    if fig._suptitle is not None and fig._suptitle.get_text():
        return fig._suptitle.get_text()
    return ""


def _clear_suptitle(fig) -> None:
    """Clear only the figure-level suptitle."""
    if fig._suptitle is not None:
        fig.suptitle("")


def _clear_figure_title(fig) -> None:
    """Remove ALL title text from a matplotlib figure (suptitle + axes titles)."""
    _clear_suptitle(fig)
    for ax in fig.get_axes():
        ax.set_title("")


def _subplot_ncols(fig) -> int | None:
    """Detect the number of columns in *fig*’s subplot gridspec, or None."""
    axes = fig.get_axes()
    if not axes:
        return None
    try:
        gs = axes[0].get_gridspec()
        if gs is not None and all(ax.get_gridspec() is gs for ax in axes):
            return gs.ncols
    except Exception:
        pass
    return None


def _save_single_axes(fig, ax, path: Path) -> None:
    """Save *ax* as a fully standalone PNG at *path*.

    The axes is extracted from *fig* by deep-copying the whole figure via
    pickle, then removing every other axes.  This guarantees that:
    - no surrounding subplot layout bleeds into the image, and
    - any title already cleared on *ax* (before this call) truly disappears.
    """
    import pickle
    import matplotlib.pyplot as plt

    # Identify which visible axes corresponds to `ax`.
    visible = [a for a in fig.get_axes() if a.get_visible()]
    ax_idx  = visible.index(ax)

    # Deep-copy via pickle so we never mutate the caller's figure.
    fig_copy     = pickle.loads(pickle.dumps(fig))
    visible_copy = [a for a in fig_copy.get_axes() if a.get_visible()]

    for i, other in enumerate(visible_copy):
        if i != ax_idx:
            fig_copy.delaxes(other)

    fig_copy.savefig(path, bbox_inches="tight")
    plt.close(fig_copy)


# ── Execution ─────────────────────────────────────────────────────────────────

def execute_block(
    code: str,
    *,
    namespace: dict,
    block_index: int,
    abs_image_dir: Path,
    rel_image_dir: Path,
    opts: dict,
    quiet: bool,
) -> str:
    """
    Run *code* inside *namespace*, capture its output, save any new matplotlib
    figures, and return the Typst markup to insert after the code block.

    *opts* is the options dict produced by ``parse_options``; image/figure
    display settings (width, height, fit, alt, format, caption) are forwarded
    to every figure generated by this block.

    Execution errors are non-fatal: the traceback is embedded as a text block
    so the rest of the document still compiles.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    open_figs  = set(plt.get_fignums()) if plt else set()

    # Replace plt.show() with a no-op so the headless Agg warning is never raised.
    # Figures are captured via plt.get_fignums() after execution instead.
    if plt:
        plt.show = lambda *a, **kw: None

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(compile(code, f"<block {block_index}>", "exec"), namespace)
    except Exception:
        error_text = traceback.format_exc()
        if not quiet:
            print(
                f"[typst-pyexec] WARNING: block {block_index} raised an exception "
                "(shown in document).",
                file=sys.stderr,
            )
        return as_raw_block(error_text.strip(), lang="text")

    parts = []

    # ---- Text output ----
    output = stdout_buf.getvalue()
    errors = stderr_buf.getvalue()
    if errors:
        combined = (output + "\n[stderr]\n" + errors) if output else ("[stderr]\n" + errors)
    else:
        combined = output
    if combined.strip():
        parts.append(as_raw_block(combined.strip(), lang="text"))

    # ---- Matplotlib figures ----
    if plt:
        new_figs = sorted(set(plt.get_fignums()) - open_figs)

        # saved: list of (image_call_str, per_image_caption | None)
        saved: list[tuple[str, str | None]] = []
        caption_from_title: str | None = None  # suptitle → block-level caption
        detected_cols: int | None = None        # auto-detected from gridspec

        for pos, fig_num in enumerate(new_figs, start=1):
            fig = plt.figure(fig_num)
            axes = [ax for ax in fig.get_axes() if ax.get_visible()]
            split = not opts["keep_subplots"] and len(axes) > 1

            # Suptitle becomes the block-level caption (first figure wins).
            suptitle = _extract_figure_title(fig)
            if suptitle and caption_from_title is None:
                caption_from_title = suptitle

            abs_image_dir.mkdir(parents=True, exist_ok=True)

            if split:
                if detected_cols is None:
                    detected_cols = _subplot_ncols(fig)
                _clear_suptitle(fig)
                for ax_pos, ax in enumerate(axes, start=1):
                    fname = f"block_{block_index:03d}_fig_{pos:02d}_ax_{ax_pos:02d}.png"
                    # Capture axes title as per-image caption, then clear it
                    # from the plot so it doesn't appear twice (title on image
                    # AND caption underneath).
                    ax_title = ax.get_title()
                    if not opts["keep_title"]:
                        ax.set_title("")
                    _save_single_axes(fig, ax, abs_image_dir / fname)
                    saved.append((_image_call((rel_image_dir / fname).as_posix(), opts),
                                  ax_title if ax_title else None))
            else:
                # For a single-axes figure, capture the axes title as the
                # block-level caption source (suptitle takes priority if set).
                if len(axes) == 1:
                    ax_title = axes[0].get_title()
                    if ax_title and caption_from_title is None:
                        caption_from_title = ax_title
                fname = f"block_{block_index:03d}_fig_{pos:02d}.png"
                if not opts["keep_title"]:
                    _clear_figure_title(fig)
                fig.savefig(abs_image_dir / fname, bbox_inches="tight")
                saved.append((_image_call((rel_image_dir / fname).as_posix(), opts),
                               suptitle if suptitle else None))

            plt.close(fig)

        if not saved:
            return "".join(parts)

        # Block-level caption: explicit %| caption > suptitle > none.
        block_cap = opts["caption"]
        if block_cap is None and not opts["keep_title"] and caption_from_title:
            block_cap = caption_from_title

        if len(saved) > 1 and opts["grid"]:
            # All images → one outer #figure(grid(figure(...), figure(...), ...)).
            # Each child figure carries its own per-image caption.
            # Column priority: grid-columns extra > columns shorthand > gridspec > count.
            _grid_extra = opts.get("_grid_extra", {})
            if "columns" in _grid_extra:
                cols = _grid_extra["columns"]
            elif opts["columns"] is not None:
                cols = opts["columns"]
            elif detected_cols is not None:
                cols = str(detected_cols)
            else:
                cols = str(len(saved))
            # Extra grid args excluding columns (already handled above).
            # Default row-gutter of 1em prevents child captions from overlapping
            # the next row; the user can override with %| grid-row-gutter: …
            extra_grid_args_parts = []
            if "row-gutter" not in _grid_extra:
                extra_grid_args_parts.append("row-gutter: 1em")
            extra_grid_args_parts += [
                f"{k}: {v}" for k, v in _grid_extra.items() if k != "columns"
            ]
            extra_grid_args = ", ".join(extra_grid_args_parts)
            grid_prefix = f"columns: {cols}" + (", " + extra_grid_args if extra_grid_args else "")
            base_label = opts.get("label")
            children = ", ".join(
                _figure_markup(
                    img, per_cap, opts.get("_fig_extra"), code_mode=True,
                    label=f"{base_label}-{i + 1}" if base_label else None
                ).strip()
                for i, (img, per_cap) in enumerate(saved)
            )
            grid_body = f"grid({grid_prefix}, {children})"
            # If there is a block-level caption, wrap the grid in a single outer
            # #figure(grid(...), caption: [...]) so the whole panel is one numbered
            # figure.  Without a caption, emit a bare #grid(...) so the children
            # are the numbered figures — avoids a spurious "Figure N" with no text.
            base_label = opts.get("label")
            if block_cap is not None:
                parts.append(_figure_markup(grid_body, block_cap, opts.get("_fig_extra"), label=base_label))
            else:
                lbl_suffix = f" <{base_label}>" if base_label else ""
                parts.append(f"#{grid_body}{lbl_suffix}\n\n")
        else:
            # Single image, or grid explicitly disabled.
            base_label = opts.get("label")
            for i, (img_call, _) in enumerate(saved):
                lbl = f"{base_label}-{i + 1}" if base_label and len(saved) > 1 else base_label
                parts.append(_figure_markup(img_call, block_cap, opts.get("_fig_extra"), label=lbl))

    return "".join(parts)


# ── Caching ───────────────────────────────────────────────────────────────────

def block_signature(options: dict, code: str) -> str:
    """Return a stable SHA-256 hex digest that changes when any option or code changes."""
    payload = json.dumps({**options, "code": code}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cache(cache_path: Path) -> dict:
    """Load cached block data from disk; return an empty cache if unavailable."""
    if not cache_path.exists():
        return {"signatures": [], "rendered": []}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        sigs     = data.get("signatures", [])
        rendered = data.get("rendered", [])
        if isinstance(sigs, list) and isinstance(rendered, list):
            return {"signatures": sigs, "rendered": rendered}
    except Exception:
        pass
    return {"signatures": [], "rendered": []}


def save_cache(cache_path: Path, signatures: list[str], rendered: list[str]) -> None:
    """Persist block signatures and rendered output for reuse on the next run."""
    cache_path.write_text(
        json.dumps({"signatures": signatures, "rendered": rendered}, ensure_ascii=True),
        encoding="utf-8",
    )


# ── Preprocessing pipeline ───────────────────────────────────────────────────

def preprocess(
    source: Path,
    output: Path,
    image_dir: Path,
    cache_path: Path,
    *,
    quiet: bool,
) -> None:
    """
    Read *source*, execute all ```python blocks, and write the result to *output*.

    *image_dir* is the path (relative to *output*) where generated PNG figures
    are stored.  If no code has changed since the last run the cached rendered
    output is reused without re-executing any Python.
    """
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    segments: list[tuple[str, object]] = []  # ("text", str) | ("block", int)
    blocks:   list[dict] = []
    block_idx = 0
    i = 0

    abs_image_dir = output.parent / image_dir

    # ---- Parse: split plain text from python fences ----
    while i < len(lines):
        if lines[i].strip() != "```python":
            segments.append(("text", rewrite_relative_paths(lines[i])))
            i += 1
            continue

        block_idx += 1
        i += 1  # skip opening fence

        raw_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != "```":
            raw_lines.append(lines[i])
            i += 1

        if i >= len(lines):
            raise SyntaxError(f"Unclosed ```python block #{block_idx} in {source}")

        i += 1  # skip closing fence

        opts, code = parse_options(raw_lines)
        blocks.append({"index": block_idx, "options": opts, "code": code})
        segments.append(("block", len(blocks) - 1))

    # ---- Per-block cache check ----
    #
    # Strategy: find the *first* block whose signature changed (first_dirty).
    # All blocks before it have identical code/options, so:
    #   - their cached rendered output is reused as-is, AND
    #   - their code is silently re-executed to restore the shared namespace state
    #     (later blocks may depend on variables they define).
    # All blocks from first_dirty onward are fully re-executed and their output
    # is regenerated.
    #
    # This means that editing block N only re-runs blocks N, N+1, N+2, …
    # leaving blocks 0 … N-1 untouched.
    signatures = [block_signature(b["options"], b["code"]) for b in blocks]
    cached     = load_cache(cache_path)

    cached_sigs     = cached["signatures"]
    cached_rendered = cached["rendered"]

    # Determine first_dirty: the index of the first block that changed.
    if (
        len(cached_sigs) == len(blocks)
        and len(cached_rendered) == len(blocks)
        and signatures == cached_sigs
    ):
        # Full cache hit – nothing to do.
        rendered_blocks: list[str] = cached_rendered
        if not quiet:
            print("[typst-pyexec] All blocks cached – skipping Python execution.")
    else:
        # Find first changed index (or 0 if block count changed).
        first_dirty = 0
        if len(cached_sigs) == len(blocks) and len(cached_rendered) == len(blocks):
            for i, (sig, csig) in enumerate(zip(signatures, cached_sigs)):
                if sig != csig:
                    first_dirty = i
                    break

        if not quiet:
            n_clean = first_dirty
            n_dirty = len(blocks) - first_dirty
            if n_clean > 0:
                print(
                    f"[typst-pyexec] {n_clean} block(s) unchanged; "
                    f"re-running block(s) {first_dirty + 1}–{len(blocks)}…"
                )
            else:
                print(f"[typst-pyexec] Running all {len(blocks)} block(s)…")

        # ---- Replay unchanged prefix to restore namespace state ----
        # We execute unchanged blocks silently (output already cached) so that
        # the shared namespace is in the correct state for the dirty blocks.
        namespace: dict = {"__name__": "__main__"}
        if plt:
            plt.show = lambda *a, **kw: None

        for block in blocks[:first_dirty]:
            if block["options"]["execute"] and block["code"].strip():
                try:
                    exec(compile(block["code"], f"<block {block['index']}>", "exec"), namespace)
                except Exception:
                    pass  # error output is already stored in cached_rendered

        # ---- Execute dirty blocks; collect fresh rendered output ----
        fresh_rendered: list[str] = []
        for block in blocks[first_dirty:]:
            rendered = ""
            opts = block["options"]
            code = block["code"]
            idx  = block["index"]

            if opts["echo"]:
                rendered += as_raw_block(code.rstrip("\n"), lang="python")

            if opts["execute"] and code.strip():
                rendered += execute_block(
                    code,
                    namespace=namespace,
                    block_index=idx,
                    abs_image_dir=abs_image_dir,
                    rel_image_dir=image_dir,
                    opts=opts,
                    quiet=quiet,
                )

            fresh_rendered.append(rendered)

        rendered_blocks = list(cached_rendered[:first_dirty]) + fresh_rendered
        save_cache(cache_path, signatures, rendered_blocks)

    # ---- Assemble the output file ----
    result: list[str] = []
    for kind, payload in segments:
        if kind == "text":
            result.append(str(payload))
        else:
            result.append(rendered_blocks[int(payload)])

    output.write_text("".join(result), encoding="utf-8")


# ── Typst integration ─────────────────────────────────────────────────────────

def compile_pdf(typ: Path, pdf: Path, root: Path) -> None:
    """Run ``typst compile <typ> <pdf>`` and raise ``RuntimeError`` on failure."""
    proc = subprocess.run(
        ["typst", "compile", str(typ), str(pdf), "--root", str(root)],
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("typst compile failed.")


def preview_on_save(
    source: Path,
    generated: Path,
    image_dir: Path,
    cache_path: Path,
    interval: float,
    host: str | None,
    port: int | None,
) -> None:
    """
    Preprocess once, launch ``tinymist preview`` for live browser preview, then
    poll the source directory for file-system changes.

    When the user saves the source file our watcher detects the mtime change,
    re-preprocesses (executing Python code blocks), and writes the updated
    intermediate file.  ``tinymist preview`` picks up that change automatically
    and refreshes the browser – no manual reload required.

    Python code blocks are therefore executed *only on save*, while the
    browser preview stays live throughout the editing session.
    """
    watch_root = source.parent.resolve()

    print("[typst-pyexec] Initial preprocessing…")
    preprocess(source, generated, image_dir, cache_path, quiet=False)

    cmd = ["tinymist", "preview", str(generated), "--root", str(source.parent)]
    if host:
        cmd += ["--host", host]
    if port is not None:
        cmd += ["--port", str(port)]

    tinymist_proc = subprocess.Popen(cmd)

    # Give tinymist ~1.5 s to bind its port; if it exits before that the port
    # is almost certainly already in use.
    time.sleep(1.5)
    rc = tinymist_proc.poll()
    if rc is not None:
        hint = (
            "\n  Hint: the port is already in use — try --port to specify a different one."
            if rc != 0 else ""
        )
        raise RuntimeError(f"`tinymist preview` failed to start (exit {rc}).{hint}")

    # Paths we write ourselves – exclude from the change detector so that
    # writing the generated file doesn't immediately trigger another pass.
    ignore_files = {generated.resolve()}
    ignore_dirs  = {(generated.parent / image_dir).resolve()}

    def snapshot() -> dict[Path, float]:
        """Collect the mtime of every user-owned file under watch_root."""
        mtimes: dict[Path, float] = {}
        for p in watch_root.rglob("*"):
            if not p.is_file():
                continue
            resolved = p.resolve()
            if resolved in ignore_files:
                continue
            if any(resolved.is_relative_to(d) for d in ignore_dirs):
                continue
            try:
                mtimes[resolved] = p.stat().st_mtime
            except OSError:
                pass
        return mtimes

    last = snapshot()
    print(f"[typst-pyexec] Watching {watch_root}  (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(interval)

            if tinymist_proc.poll() is not None:
                raise RuntimeError("`tinymist preview` exited unexpectedly.")

            current = snapshot()
            if current != last:
                print("[typst-pyexec] Change detected – re-preprocessing…")
                preprocess(source, generated, image_dir, cache_path, quiet=False)
                last = snapshot()
    finally:
        if tinymist_proc.poll() is None:
            tinymist_proc.terminate()
            try:
                tinymist_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tinymist_proc.kill()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="typst_pyexec.py",
        description="Execute Python code blocks in a Typst document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  uv run typst_pyexec.py -c report.typ       compile → report.pdf\n"
            "  uv run typst_pyexec.py -p report.typ       live preview via tinymist\n"
            "  uv run typst_pyexec.py -c -d report.typ    compile and keep generated file\n"
        ),
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-c", "--compile", action="store_true", help="Compile to PDF once.")
    mode.add_argument("-p", "--preview", action="store_true",
                      help="Live browser preview via tinymist; Python blocks re-run on save.")

    parser.add_argument("input",         type=Path,  help="Source .typ file.")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Keep the intermediate .typ file after compilation.")
    parser.add_argument("--images-dir",  type=Path,  default=Path("img"),
                        help="Image output dir inside .typst_pyexec/ (default: img).")
    parser.add_argument("--interval",    type=float, default=1.0,
                        help="Polling interval in seconds for preview mode (default: 1.0).")
    parser.add_argument("--host",        type=str,   default=None,
                        help="Host for tinymist preview (default: tinymist's default).")
    parser.add_argument("--port",        type=int,   default=None,
                        help="Port for tinymist preview (default: tinymist's default).")

    args   = parser.parse_args()
    source = args.input.resolve()

    if not source.exists():
        print(f"[typst-pyexec] ERROR: file not found: {source}", file=sys.stderr)
        return 1
    if source.suffix.lower() != ".typ":
        print("[typst-pyexec] ERROR: input must be a .typ file.", file=sys.stderr)
        return 1
    if args.images_dir.is_absolute():
        print("[typst-pyexec] ERROR: --images-dir must be a relative path.", file=sys.stderr)
        return 1

    temp_root  = source.parent / ".typst_pyexec"
    temp_root.mkdir(parents=True, exist_ok=True)
    generated  = temp_root / f"{source.stem}.generated.typ"
    cache_path = temp_root / "block-cache.json"
    pdf        = source.with_suffix(".pdf")

    try:
        if args.compile:
            preprocess(source, generated, args.images_dir, cache_path, quiet=False)
            print(f"[typst-pyexec] Preprocessed → {generated}")
            compile_pdf(generated, pdf, source.parent)
            print(f"[typst-pyexec] Compiled     → {pdf}")
        else:  # --preview
            preview_on_save(source, generated, args.images_dir, cache_path,
                            args.interval, args.host, args.port)

    except KeyboardInterrupt:
        print("\n[typst-pyexec] Stopped.")
    except Exception as exc:
        print(f"[typst-pyexec] ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        # Keep generated file in preview mode (tinymist watches it).
        # In compile mode remove it unless --debug is set.
        if not args.debug and not args.preview:
            generated.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
