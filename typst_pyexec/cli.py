"""CLI entry point for typst_pyexec."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from typst_pyexec.builder import Builder
from typst_pyexec.utils.logging import configure_logging


def _build_command(args: argparse.Namespace) -> None:
    builder = Builder(
        source=Path(args.file),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        use_cache=not args.no_cache,
        n_jobs=args.jobs,
        compiler=args.compiler,
    )
    builder.build()


def _watch_command(args: argparse.Namespace) -> None:
    builder = Builder(
        source=Path(args.file),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        use_cache=not args.no_cache,
        n_jobs=args.jobs,
        compiler=args.compiler,
    )
    builder.watch(preview_engine=args.preview_engine)


def _clean_command(args: argparse.Namespace) -> None:
    import shutil

    cache_dir = Path(".typst_pyexec")
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
        print(f"Removed {cache_dir}")
    else:
        print(f"{cache_dir} not found — nothing to clean.")


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="typst_pyexec",
        description="Reactive Python execution engine for Typst documents.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- build ---
    build_p = sub.add_parser("build", help="Build document once and exit.")
    build_p.add_argument("file", help="Path to .typ source file")
    _add_common_args(build_p)
    build_p.set_defaults(func=_build_command)

    # --- watch ---
    watch_p = sub.add_parser("watch", help="Watch for changes and rebuild.")
    watch_p.add_argument("file", help="Path to .typ source file")
    _add_common_args(watch_p)
    watch_p.add_argument(
        "--preview-engine",
        choices=["auto", "tinymist", "typst", "none"],
        default="auto",
        help=(
            "Live preview backend: auto prefers tinymist preview, then typst watch "
            "(default: auto)"
        ),
    )
    watch_p.set_defaults(func=_watch_command)

    # --- clean ---
    clean_p = sub.add_parser("clean", help="Remove .typst_pyexec/ cache directory.")
    clean_p.set_defaults(func=_clean_command)

    return parser


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory (default: same as source file)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Disable execution cache",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=-1,
        help="Number of parallel workers (-1 = all CPUs, default: -1)",
    )
    p.add_argument(
        "--compiler",
        default="typst",
        help="Typst compiler command (default: typst)",
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``typst_pyexec`` CLI."""
    parser = _make_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(logging, args.log_level))
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
