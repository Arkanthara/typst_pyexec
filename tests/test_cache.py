"""Tests for typst_pyexec.core.cache."""

from pathlib import Path

import pytest

from typst_pyexec.core.cache import CacheStore
from typst_pyexec.utils.hashing import sha256_text


@pytest.fixture
def cache(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / "cache")


def test_miss_on_empty_cache(cache: CacheStore):
    assert cache.load("cell_1") is None


def test_load_by_hash_miss(cache: CacheStore):
    assert cache.load_by_hash("abc123") is None


def test_save_and_load(cache: CacheStore):
    result = {
        "stdout": "hello",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    cache.save("cell_1", "print('hello')", result)
    loaded = cache.load("cell_1")
    assert loaded is not None
    assert loaded["stdout"] == "hello"
    assert loaded["status"] == "ok"


def test_hash_key(cache: CacheStore):
    source = "x = 42"
    result = {
        "stdout": "",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    cache.save("cell_1", source, result)
    h = sha256_text(source)
    loaded = cache.load_by_hash(h)
    assert loaded is not None
    assert loaded["hash"] == h


def test_overwrite_on_rehash(cache: CacheStore):
    result_old = {
        "stdout": "old",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    result_new = {
        "stdout": "new",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    cache.save("c1", "x = 1", result_old)
    cache.save("c1", "x = 2", result_new)  # different source → different hash
    loaded = cache.load("c1")
    assert loaded["stdout"] == "new"


def test_invalidate(cache: CacheStore):
    result = {
        "stdout": "hi",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    cache.save("c1", "pass", result)
    cache.invalidate("c1")
    assert cache.load("c1") is None


def test_clear(cache: CacheStore):
    result = {
        "stdout": "x",
        "stderr": "",
        "display_data": [],
        "figures": [],
        "error": None,
        "status": "ok",
    }
    cache.save("c1", "a = 1", result)
    cache.save("c2", "b = 2", result)
    cache.clear()
    assert cache.load("c1") is None
    assert cache.load("c2") is None


def test_figures_stored(cache: CacheStore):
    result = {
        "stdout": "",
        "stderr": "",
        "display_data": [],
        "figures": ["/path/fig1.svg"],
        "figure_metadata": [{"path": "/path/fig1.svg", "title": "My title"}],
        "error": None,
        "status": "ok",
    }
    cache.save("c1", "plt.show()", result)
    loaded = cache.load("c1")
    assert "/path/fig1.svg" in loaded["figures"]
    assert loaded["figure_metadata"][0]["title"] == "My title"
