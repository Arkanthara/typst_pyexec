"""Tests for typst_pyexec.core.kernel serialization helpers."""

from typst_pyexec.core.kernel import (
    _default_timeout_seconds,
    _deserialize_connection_info,
    _serialize_connection_info,
)


def test_connection_info_roundtrip_bytes_key() -> None:
    original = {
        "ip": "127.0.0.1",
        "transport": "tcp",
        "shell_port": 57501,
        "key": b"secret-bytes-key",
        "signature_scheme": "hmac-sha256",
    }

    encoded = _serialize_connection_info(original)
    assert isinstance(encoded["key"], dict)
    assert encoded["key"]["__typst_pyexec_bytes__"] is True

    decoded = _deserialize_connection_info(encoded)
    assert decoded["ip"] == original["ip"]
    assert decoded["shell_port"] == original["shell_port"]
    assert decoded["key"] == original["key"]


def test_default_timeout_seconds_uses_default_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("TYPST_PYEXEC_CELL_TIMEOUT", raising=False)
    assert _default_timeout_seconds() == 600.0


def test_default_timeout_seconds_parses_env(monkeypatch) -> None:
    monkeypatch.setenv("TYPST_PYEXEC_CELL_TIMEOUT", "75")
    assert _default_timeout_seconds() == 75.0


def test_default_timeout_seconds_rejects_invalid(monkeypatch) -> None:
    monkeypatch.setenv("TYPST_PYEXEC_CELL_TIMEOUT", "abc")
    assert _default_timeout_seconds() == 600.0


def test_default_timeout_seconds_rejects_non_positive(monkeypatch) -> None:
    monkeypatch.setenv("TYPST_PYEXEC_CELL_TIMEOUT", "0")
    assert _default_timeout_seconds() == 600.0
