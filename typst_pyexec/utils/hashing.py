"""SHA-256 hashing helpers."""

from __future__ import annotations

import hashlib


def sha256_text(text: str) -> str:
    """Return the hex-encoded SHA-256 digest of *text* (UTF-8 encoded).

    Parameters
    ----------
    text:
        The string to hash.

    Returns
    -------
    str
        64-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of raw *data*."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """Return the hex-encoded SHA-256 digest of the file at *path*.

    Reads the file in binary mode using a streaming approach to avoid
    loading large files into memory at once.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
