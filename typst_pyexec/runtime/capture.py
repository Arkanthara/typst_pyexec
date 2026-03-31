"""Stdout / figure / DataFrame capture utilities used inside the kernel."""

from __future__ import annotations

import io
import sys

import pandas as pd
from collections.abc import Generator
from contextlib import contextmanager


@contextmanager
def capture_stdout() -> Generator[io.StringIO, None, None]:
    """Context manager that captures stdout into a :class:`io.StringIO` buffer.

    Usage::

        with capture_stdout() as buf:
            print("hello")
        text = buf.getvalue()  # "hello\\n"
    """
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def dataframe_to_typst(df: pd.DataFrame) -> str:  # type: ignore[name-defined]  # noqa: F821
    """Convert a pandas DataFrame to a Typst ``#table(…)`` expression.

    Parameters
    ----------
    df:
        The DataFrame to convert.

    Returns
    -------
    str
        A Typst table expression with a header row.
    """
    cols = list(df.columns)
    ncols = len(cols)
    header = ", ".join(f"[*{c}*]" for c in cols)
    rows = ""
    for _, row in df.iterrows():
        rows += ", ".join(f"[{row[c]}]" for c in cols) + ",\n"
    return f"#table(\n  columns: {ncols},\n  {header},\n  {rows})\n"
