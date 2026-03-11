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

Subfigures use (a), (b), … notation by default. Labels are <label>a, <label>b.
To customize subfigure appearance, use a Typst show rule on the generated
`subfigure` function:
  #let subfigure = figure.with(kind: "subfigure", supplement: none, numbering: "(a)")
Override numbering, supplement, etc. via %| fig-xxx passthrough.

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
    named = {"kind": '"subfigure"', "supplement": "none", "numbering": '"(a)"'}
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
                  abs_dir: Path, rel_dir: Path, opts: dict) -> str:
    """Execute code, capture output and figures, return Typst markup."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    open_figs = set(plt.get_fignums()) if plt else set()
    if plt:
        plt.show = lambda *a, **kw: None

    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exec(compile(code, f"<block {idx}>", "exec"), ns)
    except Exception:
        return _raw(traceback.format_exc().strip(), lang="text")

    parts = []

    # Text output
    out = buf_out.getvalue()
    err = buf_err.getvalue()
    text = (out + "\n[stderr]\n" + err) if err else out
    if text.strip():
        parts.append(_raw(text.strip(), lang="text"))

    # Matplotlib figures
    if not plt:
        return "".join(parts)

    new_figs = sorted(set(plt.get_fignums()) - open_figs)
    if not new_figs:
        return "".join(parts)

    # Collect all images: list of (image_call, per_image_caption)
    images: list[tuple[str, str | None]] = []
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
                images.append((_image((rel_dir / fname).as_posix(), opts["_img"]),
                               ax_title or None))
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
            images.append((_image((rel_dir / fname).as_posix(), opts["_img"]),
                           suptitle or None))
        plt.close(fig)

    if not images:
        return "".join(parts)

    # Resolve caption: explicit > suptitle > none
    cap = opts["caption"]
    if cap is None and not opts["keep_title"] and block_caption:
        cap = block_caption
    label = opts["label"]

    if len(images) == 1:
        # Single image → single figure
        parts.append(_figure(images[0][0], opts["_fig"], caption=cap, label=label))
    else:
        # Multiple images → grid of subfigures inside an outer figure
        ncols = detected_cols or len(images)
        # Override from grid-columns if specified
        grid_extra = dict(opts["_grid"])
        if "columns" in grid_extra:
            ncols_override = grid_extra.pop("columns")
            ncols = int(ncols_override) if ncols_override.isdigit() else ncols

        children = []
        for i, (img, per_cap) in enumerate(images):
            letter = _ALPHA[i] if i < 26 else str(i + 1)
            sub_label = f"{label}{letter}" if label else None
            children.append(_subfigure(img, opts["_fig"],
                                       caption=per_cap, label=sub_label))
        grid_body = _grid(children, grid_extra, ncols)
        parts.append(_figure(grid_body, {}, caption=cap, label=label))

    return "".join(parts)


# ── Caching ───────────────────────────────────────────────────────────────────

def _sig(opts: dict, code: str) -> str:
    """SHA-256 hash of block options + code."""
    payload = json.dumps({**opts, "code": code}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_cache(path: Path) -> tuple[list[str], list[str]]:
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            s, r = d.get("sigs", []), d.get("rendered", [])
            if isinstance(s, list) and isinstance(r, list):
                return s, r
        except Exception:
            pass
    return [], []


def _save_cache(path: Path, sigs: list[str], rendered: list[str]) -> None:
    path.write_text(json.dumps({"sigs": sigs, "rendered": rendered}), encoding="utf-8")


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

    # Compute signatures and find first dirty block
    sigs = [_sig(b["opts"], b["code"]) for b in blocks]
    cached_sigs, cached_rendered = _load_cache(cache_path)

    if cached_sigs == sigs and len(cached_rendered) == len(blocks):
        rendered = cached_rendered
        print("[typst-py] All blocks cached.")
    else:
        dirty = 0
        if len(cached_sigs) == len(blocks) and len(cached_rendered) == len(blocks):
            for j, (s, cs) in enumerate(zip(sigs, cached_sigs)):
                if s != cs:
                    dirty = j
                    break

        n = len(blocks)
        if dirty > 0:
            print(f"[typst-py] {dirty} cached, re-running {dirty+1}–{n}…")
        else:
            print(f"[typst-py] Running all {n} block(s)…")

        # Replay unchanged blocks to restore namespace
        ns: dict = {"__name__": "__main__"}
        abs_dir = output.parent / img_dir
        if plt:
            plt.show = lambda *a, **kw: None
        for b in blocks[:dirty]:
            if b["opts"]["execute"] and b["code"].strip():
                try:
                    exec(compile(b["code"], f"<block {b['idx']}>", "exec"), ns)
                except Exception:
                    pass

        # Execute dirty blocks
        fresh = []
        for b in blocks[dirty:]:
            r = ""
            if b["opts"]["echo"]:
                r += _raw(b["code"].rstrip("\n"), lang="python")
            if b["opts"]["execute"] and b["code"].strip():
                r += execute_block(b["code"], ns=ns, idx=b["idx"],
                                   abs_dir=abs_dir, rel_dir=img_dir, opts=b["opts"])
            fresh.append(r)

        rendered = list(cached_rendered[:dirty]) + fresh
        _save_cache(cache_path, sigs, rendered)

    # Assemble output
    out = []
    for kind, payload in segments:
        out.append(str(payload) if kind == "text" else rendered[int(payload)])
    output.write_text("".join(out), encoding="utf-8")


# ── Watch mode ────────────────────────────────────────────────────────────────

def watch(source: Path, generated: Path, img_dir: Path, cache_path: Path,
          interval: float) -> None:
    """Preprocess on save, launch `typst watch` for live compilation."""
    print("[typst-py] Initial preprocessing…")
    preprocess(source, generated, img_dir, cache_path)

    # Launch typst watch on the generated file
    cmd = ["typst", "watch", str(generated), "--root", str(source.parent)]
    proc = subprocess.Popen(cmd)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("`typst watch` failed to start.")

    # Track file changes in source directory
    watch_root = source.parent.resolve()
    ignore = {generated.resolve()}
    ignore_dirs = {(generated.parent / img_dir).resolve()}

    def snap() -> dict[Path, float]:
        m: dict[Path, float] = {}
        for p in watch_root.rglob("*"):
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp in ignore or any(rp.is_relative_to(d) for d in ignore_dirs):
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
                raise RuntimeError("`typst watch` exited unexpectedly.")
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
