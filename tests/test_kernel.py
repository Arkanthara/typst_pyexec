"""Tests for typst_pyexec.core.kernel serialization helpers."""

from typst_pyexec.core.kernel import (
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
