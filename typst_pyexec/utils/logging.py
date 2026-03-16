"""Logging configuration for typst_pyexec."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root ``typst_pyexec`` logger.

    Sets up a :class:`logging.StreamHandler` that writes to ``stderr``
    with a concise, readable format.

    Parameters
    ----------
    level:
        One of :data:`logging.DEBUG`, :data:`logging.INFO`,
        :data:`logging.WARNING`, or :data:`logging.ERROR`.
    """
    root = logging.getLogger("typst_pyexec")
    if root.handlers:
        return  # already configured

    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``typst_pyexec`` namespace."""
    return logging.getLogger(f"typst_pyexec.{name}")
