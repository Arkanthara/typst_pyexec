#!/usr/bin/env python3
"""
typst_py.py – Execute Python code blocks embedded in a Typst document.

Reads a .typ source, finds every ```python … ``` fenced block, executes the
code in a shared namespace, and replaces each fence with:
  - source listing      (unless %| echo: false)
  - printed output      (when execute: true)
  - matplotlib figures  (saved as PNG, inserted as #figure/#grid)

Per-block options  (place at the very top of the block)
------------------
  %| execute: false        skip execution
  %| echo: false           hide source listing
  %| caption: My figure    block-level caption (overrides auto-detection)
  %| label: fig-name       Typst label for @fig-name cross-references
  %| keep-title: true      keep matplotlib titles on plots
  %| keep-subplots: true   keep plt.subplots() as a single image

  %| img-xxx: <value>      forwarded as  xxx: <value>  inside image()
  %| fig-xxx: <value>      forwarded as  xxx: <value>  inside figure()
  %| grid-xxx: <value>     forwarded as  xxx: <value>  inside grid()

Subfigures are emitted as figure(kind: "subfigure"). Labels are <label>a, <label>b.
Caption formatting – bold "(a) caption" and counter reset per outer figure –
requires these show rules in your Typst template:

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
  uv run typst_py.py -c report.typ       compile → report.pdf
  uv run typst_py.py -w report.typ       watch + live preview via typst watch

Dependencies: standard library only.  matplotlib is optional.
"""

import argparse, hashlib, io, json, pickle, subprocess, sys, textwrap
import time, traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

# ── Parsing ───────────────────────────────────────────────────────────────────

_BOOL_OPTS = {"execute", "echo", "keep_title", "keep_subplots"}


def parse_options(lines: list[str]) -> tuple[dict, str]:
    """Extract %| options from the top of a code block. Returns (opts, code)."""
    opts = {
        "execute": True, "echo": True,
        "keep_title": False, "keep_subplots": False,
        "caption": None, "label": None,
        "_img": {}, "_fig": {}, "_grid": {},
    }
    start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("%|") and ":" in s[2:]:
            key, _, val = s[2:].partition(":")
            key = key.strip().lower().replace("-", "_")
            val = val.strip()
            if key.startswith("img_"):
                opts["_img"][key[4:].replace("_", "-")] = val
            elif key.startswith("fig_"):
                opts["_fig"][key[4:].replace("_", "-")] = val
            elif key.startswith("grid_"):
                opts["_grid"][key[5:].replace("_", "-")] = val
            elif key in _BOOL_OPTS:
                opts[key] = val.lower() not in ("false", "no", "0", "off")
            elif key in opts:
                opts[key] = None if val.lower() == "none" else val
            start = i + 1
        elif s.startswith("%"):
            start = i + 1
        else:
            start = i
            break
    else:
        start = len(lines)
    return opts, textwrap.dedent("".join(lines[start:]))


# ── Typst output helpers ─────────────────────────────────────────────────────

def _raw(text: str, lang: str = "") -> str:
    """Wrap text in a Typst #raw(…) call."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if lang:
        return f'#raw("{esc}", block: true, lang: "{lang}")\n\n'
    return f'#raw("{esc}", block: true)\n\n'


def _args(pairs: dict) -> str:
    """Format a dict as Typst named arguments: key: val, key: val."""
    return ", ".join(f"{k}: {v}" for k, v in pairs.items())


def _image(path: str, extra: dict) -> str:
    """Build image("path", ...) call."""
    parts = [f'"{path}"']
    if extra:
        parts.append(_args(extra))
    return f'image({", ".join(parts)})'


def _figure(body: str, extra: dict, *, caption: str | None = None,
            label: str | None = None, code_mode: bool = False) -> str:
    """Build figure(body, ...) <label> markup."""
    named = dict(extra)
    if caption is not None and "caption" not in named:
        named["caption"] = f"[{caption}]"
    parts = [body]
    if named:
        parts.append(_args(named))
    inner = f'figure({", ".join(parts)})'
    lbl = f" <{label}>" if label else ""
    if code_mode:
        return f"[#{inner}{lbl}]"
    return f"#{inner}{lbl}\n\n"


def _subfigure(body: str, extra: dict, *, caption: str | None = None,
               label: str | None = None) -> str:
    """Build a subfigure (figure with kind: "subfigure") for grid children."""
    named = {"kind": '"subfigure"'}
    named.update(extra)
    if caption is not None and "caption" not in named:
        named["caption"] = f"[{caption}]"
    parts = [body]
    if named:
        parts.append(_args(named))
    inner = f'figure({", ".join(parts)})'
    lbl = f" <{label}>" if label else ""
    return f"[#{inner}{lbl}]"


def _grid(children: list[str], extra: dict, ncols: int) -> str:
    """Build grid(columns: N, ..., child1, child2, ...) call."""
    named = {"columns": str(ncols)}
    named.update(extra)
    parts = [_args(named)] + children
    return f'grid({", ".join(parts)})'


def _rewrite_path(line: str) -> str:
    """Remap relative paths in #import/#include for the generated file directory."""
    s = line.lstrip()
    if not (s.startswith("#import") or s.startswith("#include")):
        return line
    q1 = line.find('"')
    if q1 == -1:
        return line
    q2 = line.find('"', q1 + 1)
    if q2 == -1:
        return line
    p = line[q1 + 1:q2]
    if p.startswith(("/", "./", "../", "@")) or "://" in p:
        return line
    return line[:q1 + 1] + "../" + p + line[q2:]


# ── LaTeX → Typst title cleanup ──────────────────────────────────────────────

def _clean_title(title: str) -> str:
    """Strip LaTeX markup from matplotlib titles for use as Typst captions."""
    if '\\' in title:
        title = title.replace('\\', '').replace('{', '(').replace('}', ')')
    return title


# ── Matplotlib helpers ────────────────────────────────────────────────────────

def _get_suptitle(fig) -> str:
    if fig._suptitle is not None and fig._suptitle.get_text():
        return _clean_title(fig._suptitle.get_text())
    return ""


def _detect_ncols(fig) -> int | None:
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


def _save_axes(fig, ax, path: Path) -> None:
    """Save a single axes as a standalone image by deep-copying the figure."""
    visible = [a for a in fig.get_axes() if a.get_visible()]
    idx = visible.index(ax)
    fig_copy = pickle.loads(pickle.dumps(fig))
    for i, other in enumerate(a for a in fig_copy.get_axes() if a.get_visible()):
        if i != idx:
            fig_copy.delaxes(other)
    fig_copy.savefig(path, bbox_inches="tight")
    plt.close(fig_copy)


# ── Block execution ──────────────────────────────────────────────────────────

_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def execute_block(code: str, *, ns: dict, idx: int,
                  abs_dir: Path, rel_dir: Path, opts: dict) -> tuple[str, dict]:
    """Execute code, capture output and figures. Returns (Typst markup, exec_data).

    exec_data stores raw results to allow re-rendering without re-running Python:
    {"text", "images": [{"path", "caption"}], "ncols", "block_caption"}.
    """
    _empty: dict = {"text": "", "images": [], "ncols": None, "block_caption": None}
    buf_out, buf_err = io.StringIO(), io.StringIO()
    open_figs = set(plt.get_fignums()) if plt else set()
    if plt:
        plt.show = lambda *a, **kw: None

    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compile(code, f"<block {idx}>", "exec"), ns)
    except Exception:
        tb = traceback.format_exc().strip()
        return _raw(tb, lang="text"), {**_empty, "text": tb}

    parts = []

    # Text output
    out = buf_out.getvalue()
    err = buf_err.getvalue()
    text = (out + "\n[stderr]\n" + err) if err else out
    if text.strip():
        parts.append(_raw(text.strip(), lang="text"))

    # Matplotlib figures
    if not plt:
        return "".join(parts), {**_empty, "text": text}

    new_figs = sorted(set(plt.get_fignums()) - open_figs)
    if not new_figs:
        return "".join(parts), {**_empty, "text": text}

    # Collect images: list of (rel_path_posix, per_image_caption)
    raw_images: list[tuple[str, str | None]] = []
    block_caption: str | None = None
    detected_cols: int | None = None
    abs_dir.mkdir(parents=True, exist_ok=True)

    for pos, fnum in enumerate(new_figs, 1):
        fig = plt.figure(fnum)
        axes = [ax for ax in fig.get_axes() if ax.get_visible()]
        split = not opts["keep_subplots"] and len(axes) > 1

        suptitle = _get_suptitle(fig)
        if suptitle and block_caption is None:
            block_caption = suptitle

        if split:
            if detected_cols is None:
                detected_cols = _detect_ncols(fig)
            # Clear suptitle from the figure before saving individual axes
            if fig._suptitle is not None:
                fig.suptitle("")
            for ai, ax in enumerate(axes, 1):
                ax_title = _clean_title(ax.get_title())
                if not opts["keep_title"]:
                    ax.set_title("")
                fname = f"b{idx}_f{pos}_a{ai}.png"
                _save_axes(fig, ax, abs_dir / fname)
                raw_images.append(((rel_dir / fname).as_posix(), ax_title or None))
        else:
            if len(axes) == 1:
                ax_title = _clean_title(axes[0].get_title())
                if ax_title and block_caption is None:
                    block_caption = ax_title
            if not opts["keep_title"]:
                if fig._suptitle is not None:
                    fig.suptitle("")
                for ax in axes:
                    ax.set_title("")
            fname = f"b{idx}_f{pos}.png"
            fig.savefig(abs_dir / fname, bbox_inches="tight")
            raw_images.append(((rel_dir / fname).as_posix(), suptitle or None))
        plt.close(fig)

    edata: dict = {
        "text": text,
        "images": [{"path": p, "caption": c} for p, c in raw_images],
        "ncols": detected_cols,
        "block_caption": block_caption,
    }

    if not raw_images:
        return "".join(parts), edata

    parts += _build_figure_markup(raw_images, detected_cols, block_caption, opts)
    return "".join(parts), edata


def _build_figure_markup(images: list[tuple[str, str | None]],
                         detected_cols: int | None,
                         block_caption: str | None,
                         opts: dict) -> list[str]:
    """Build Typst figure/grid markup from raw image paths + current display opts."""
    cap = opts["caption"]
    if cap is None and not opts["keep_title"] and block_caption:
        cap = block_caption
    label = opts["label"]

    if len(images) == 1:
        return [_figure(_image(images[0][0], opts["_img"]), opts["_fig"],
                        caption=cap, label=label)]

    # Multiple images → grid of subfigures inside an outer figure
    ncols = detected_cols or len(images)
    grid_extra = dict(opts["_grid"])
    if "columns" in grid_extra:
        ncols_override = grid_extra.pop("columns")
        ncols = int(ncols_override) if ncols_override.isdigit() else ncols

    children = []
    for i, (path, per_cap) in enumerate(images):
        letter = _ALPHA[i] if i < 26 else str(i + 1)
        sub_label = f"{label}{letter}" if label else None
        children.append(_subfigure(_image(path, opts["_img"]), opts["_fig"],
                                   caption=per_cap, label=sub_label))
    grid_body = _grid(children, grid_extra, ncols)
    return [_figure(grid_body, {"kind": "image"}, caption=cap, label=label)]


def _render_from_exec_data(edata: dict, opts: dict) -> str:
    """Reconstruct execute_block markup from cached exec_data + new display opts."""
    parts = []
    text = edata.get("text", "")
    if text.strip():
        parts.append(_raw(text.strip(), lang="text"))
    raw_images = [(d["path"], d.get("caption")) for d in edata.get("images", [])]
    if raw_images:
        parts += _build_figure_markup(
            raw_images, edata.get("ncols"), edata.get("block_caption"), opts)
    return "".join(parts)


# ── Caching ───────────────────────────────────────────────────────────────────

def _sig(code: str) -> str:
    """SHA-256 of the Python code only (ignores all %| options)."""
    return hashlib.sha256(code.encode()).hexdigest()


def _opts_sig(opts: dict) -> str:
    """SHA-256 of all %| options."""
    payload = json.dumps(opts, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_cache(path: Path) -> tuple[list[str], list[str], list[str], list[dict]]:
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            s  = d.get("sigs", [])
            os = d.get("opts_sigs", [])
            r  = d.get("rendered", [])
            e  = d.get("exec_data", [])
            if all(isinstance(x, list) for x in (s, os, r, e)):
                return s, os, r, e
        except Exception:
            pass
    return [], [], [], []


def _save_cache(path: Path, sigs: list[str], opts_sigs: list[str],
                rendered: list[str], exec_data: list[dict]) -> None:
    path.write_text(json.dumps({
        "sigs": sigs, "opts_sigs": opts_sigs,
        "rendered": rendered, "exec_data": exec_data,
    }), encoding="utf-8")


# ── Preprocessing ─────────────────────────────────────────────────────────────

def preprocess(source: Path, output: Path, img_dir: Path, cache_path: Path) -> None:
    """Parse source, execute changed blocks, write generated .typ file."""
    text = source.read_text(encoding="utf-8").splitlines(keepends=True)
    segments: list[tuple[str, object]] = []  # ("text", line) | ("block", idx)
    blocks: list[dict] = []
    i, bidx = 0, 0

    while i < len(text):
        if text[i].strip() != "```python":
            segments.append(("text", _rewrite_path(text[i])))
            i += 1
            continue
        bidx += 1
        i += 1
        raw = []
        while i < len(text) and text[i].strip() != "```":
            raw.append(text[i])
            i += 1
        if i >= len(text):
            raise SyntaxError(f"Unclosed ```python block #{bidx} in {source}")
        i += 1
        opts, code = parse_options(raw)
        blocks.append({"idx": bidx, "opts": opts, "code": code})
        segments.append(("block", len(blocks) - 1))

    # Compute signatures
    sigs      = [_sig(b["code"]) for b in blocks]
    opts_sigs = [_opts_sig(b["opts"]) for b in blocks]
    cached_sigs, cached_opts_sigs, cached_rendered, cached_exec_data = _load_cache(cache_path)

    n = len(blocks)
    rendered: list[str] = []
    exec_data_list: list[dict] = []

    # ── Case 1: nothing changed ───────────────────────────────────────────────
    if (cached_sigs == sigs and cached_opts_sigs == opts_sigs
            and len(cached_rendered) == n):
        print("[typst-py] All blocks cached.")
        rendered = cached_rendered
        exec_data_list = cached_exec_data

    # ── Case 2: only opts changed — re-render without re-running Python ───────
    elif (cached_sigs == sigs
          and len(cached_rendered) == n and len(cached_exec_data) == n):
        print("[typst-py] Options changed, re-rendering…")
        exec_data_list = cached_exec_data
        for j, b in enumerate(blocks):
            if opts_sigs[j] == cached_opts_sigs[j]:
                rendered.append(cached_rendered[j])
            else:
                r = ""
                if b["opts"]["echo"]:
                    r += _raw(b["code"].rstrip("\n"), lang="python")
                if b["opts"]["execute"]:
                    r += _render_from_exec_data(cached_exec_data[j], b["opts"])
                rendered.append(r)

    # ── Case 3: code changed — find first dirty block and re-execute ──────────
    else:
        first_dirty = 0
        if len(cached_sigs) == n and len(cached_rendered) == n and len(cached_exec_data) == n:
            for j, (s, cs) in enumerate(zip(sigs, cached_sigs)):
                if s != cs:
                    first_dirty = j
                    break

        if first_dirty > 0:
            print(f"[typst-py] {first_dirty} cached, re-running {first_dirty+1}–{n}…")
        else:
            print(f"[typst-py] Running all {n} block(s)…")

        ns: dict = {"__name__": "__main__"}
        abs_dir = output.parent / img_dir
        if plt:
            plt.show = lambda *a, **kw: None

        # Replay pre-dirty blocks to restore shared namespace
        for b in blocks[:first_dirty]:
            if b["opts"]["execute"] and b["code"].strip():
                try:
                    exec(compile(b["code"], f"<block {b['idx']}>", "exec"), ns)
                except Exception:
                    pass

        # Pre-dirty blocks: reuse cached results, re-render if their opts changed
        for j in range(first_dirty):
            b = blocks[j]
            exec_data_list.append(cached_exec_data[j])
            cached_os = cached_opts_sigs[j] if j < len(cached_opts_sigs) else None
            if opts_sigs[j] == cached_os:
                rendered.append(cached_rendered[j])
            else:
                r = ""
                if b["opts"]["echo"]:
                    r += _raw(b["code"].rstrip("\n"), lang="python")
                if b["opts"]["execute"]:
                    r += _render_from_exec_data(cached_exec_data[j], b["opts"])
                rendered.append(r)

        # Execute dirty blocks
        for b in blocks[first_dirty:]:
            r = ""
            if b["opts"]["echo"]:
                r += _raw(b["code"].rstrip("\n"), lang="python")
            if b["opts"]["execute"] and b["code"].strip():
                markup, edata = execute_block(b["code"], ns=ns, idx=b["idx"],
                                              abs_dir=abs_dir, rel_dir=img_dir,
                                              opts=b["opts"])
                r += markup
            else:
                edata = {"text": "", "images": [], "ncols": None, "block_caption": None}
            rendered.append(r)
            exec_data_list.append(edata)

    _save_cache(cache_path, sigs, opts_sigs, rendered, exec_data_list)

    # Assemble output
    out = []
    for kind, payload in segments:
        out.append(str(payload) if kind == "text" else rendered[int(payload)])
    output.write_text("".join(out), encoding="utf-8")


# ── Watch mode ────────────────────────────────────────────────────────────────

def watch(source: Path, generated: Path, img_dir: Path, cache_path: Path,
          interval: float) -> None:
    """Preprocess on save, launch `tinymist preview` for live preview."""
    print("[typst-py] Initial preprocessing…")
    preprocess(source, generated, img_dir, cache_path)

    # Launch tinymist preview on the generated file (serves HTML with live reload)
    cmd = ["tinymist", "preview", str(generated), "--root", str(source.parent)]
    proc = subprocess.Popen(cmd)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("`tinymist preview` failed to start.")

    # Track file changes in source directory, ignoring all generated artifacts
    watch_root = source.parent.resolve()
    ignore_dirs = {generated.parent.resolve()}

    def snap() -> dict[Path, float]:
        m: dict[Path, float] = {}
        for p in watch_root.rglob("*"):
            if not p.is_file():
                continue
            rp = p.resolve()
            if any(rp.is_relative_to(d) for d in ignore_dirs):
                continue
            try:
                m[rp] = p.stat().st_mtime
            except OSError:
                pass
        return m

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
                preprocess(source, generated, img_dir, cache_path)
                last = snap()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="typst_py.py",
        description="Execute Python blocks in a Typst document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  uv run typst_py.py -c report.typ    compile → report.pdf\n"
               "  uv run typst_py.py -w report.typ    watch + live preview\n",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("-c", "--compile", action="store_true", help="Compile to PDF.")
    mode.add_argument("-w", "--watch", action="store_true",
                      help="Watch for changes, preprocess on save, typst watch.")
    ap.add_argument("input", type=Path, help="Source .typ file.")
    ap.add_argument("-d", "--debug", action="store_true",
                    help="Keep intermediate .typ file.")
    ap.add_argument("--images-dir", type=Path, default=Path("img"),
                    help="Image dir inside .typst_py/ (default: img).")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Polling interval in seconds (default: 1.0).")
    args = ap.parse_args()

    source = args.input.resolve()
    if not source.exists():
        print(f"[typst-py] ERROR: {source} not found.", file=sys.stderr)
        return 1
    if source.suffix.lower() != ".typ":
        print("[typst-py] ERROR: input must be a .typ file.", file=sys.stderr)
        return 1

    tmp = source.parent / ".typst_py"
    tmp.mkdir(parents=True, exist_ok=True)
    generated = tmp / f"{source.stem}.generated.typ"
    cache = tmp / "block-cache.json"
    pdf = source.with_suffix(".pdf")

    try:
        if args.compile:
            preprocess(source, generated, args.images_dir, cache)
            print(f"[typst-py] Preprocessed → {generated}")
            r = subprocess.run(
                ["typst", "compile", str(generated), str(pdf),
                 "--root", str(source.parent)], check=False)
            if r.returncode != 0:
                raise RuntimeError("typst compile failed.")
            print(f"[typst-py] Compiled → {pdf}")
        else:
            watch(source, generated, args.images_dir, cache, args.interval)
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
