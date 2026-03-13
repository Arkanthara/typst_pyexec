"""Custom display hook injected into the Jupyter kernel namespace.

When installed, this hook intercepts the last expression value in a cell
and emits it via ``IPython.display`` so that the executor can capture it
as a MIME bundle.
"""

from __future__ import annotations

import sys
from typing import Any


class TypstDisplayHook:
    """Minimal display hook that calls IPython's ``display()`` for cell results.

    Install via::

        sys.displayhook = TypstDisplayHook()
    """

    def __call__(self, value: Any) -> None:
        if value is None:
            return
        # Try IPython display first
        try:
            from IPython.display import display

            display(value)
        except ImportError:
            # Fall back to repr
            print(repr(value))
        # Suppress assignment to builtins._
        # (standard behaviour in interactive mode)


def install() -> None:
    """Replace sys.displayhook with :class:`TypstDisplayHook`."""
    sys.displayhook = TypstDisplayHook()
