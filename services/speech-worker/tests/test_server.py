"""Fail-closed Worker listener and TLS configuration tests."""

from pathlib import Path

import pytest

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.server import ServerConfig, serve


def _tls_files(tmp_path: Path) -> tuple[Path, Path]:
    certificate = tmp_path / "worker.crt"
    private_key = tmp_path / "worker.key"
    certificate.write_text("test certificate", encoding="utf-8")
    private_key.write_text("test private key", encoding="utf-8")
    private_key.chmod(0o600)
    return certificate, private_key


def test_loopback_is_the_only_plain_http_listener() -> None:
    assert ServerConfig(data_dir=Path("runtime"), host="127.0.0.1").validated().host == (
        "127.0.0.1"
    )
    assert ServerConfig(data_dir=Path("runtime"), host="::1").validated().host == "::1"
    assert ServerConfig(data_dir=Path("runtime"), host="localhost").validated().host == (
        "localhost"
    )
    for unsafe_host in ("0.0.0.0", "::", "192.168.1.20", "100.64.0.10", "example.com"):
        with pytest.raises(InvalidJobRequest):
            ServerConfig(data_dir=Path("runtime"), host=unsafe_host).validated()


def test_private_network_bind_requires_complete_protected_tls_files(tmp_path) -> None:
    certificate, private_key = _tls_files(tmp_path)
    validated = ServerConfig(
        data_dir=tmp_path / "runtime",
        host="100.64.0.10",
        ssl_certfile=certificate,
        ssl_keyfile=private_key,
    ).validated()

    assert validated.host == "100.64.0.10"
    assert validated.ssl_certfile == certificate.resolve()
    assert validated.ssl_keyfile == private_key.resolve()

    with pytest.raises(InvalidJobRequest):
        ServerConfig(
            data_dir=tmp_path / "runtime",
            ssl_certfile=certificate,
        ).validated()

    private_key.chmod(0o644)
    with pytest.raises(InvalidJobRequest):
        ServerConfig(
            data_dir=tmp_path / "runtime",
            host="192.168.1.20",
            ssl_certfile=certificate,
            ssl_keyfile=private_key,
        ).validated()


def test_public_wildcard_or_hostname_bind_is_rejected_even_with_tls(tmp_path) -> None:
    certificate, private_key = _tls_files(tmp_path)
    for unsafe_host in ("0.0.0.0", "::", "8.8.8.8", "worker.example.com"):
        with pytest.raises(InvalidJobRequest):
            ServerConfig(
                data_dir=tmp_path / "runtime",
                host=unsafe_host,
                ssl_certfile=certificate,
                ssl_keyfile=private_key,
            ).validated()


def test_server_wires_persistent_security_and_disables_identifying_access_logs(tmp_path) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_runner(app, **kwargs) -> None:
        calls.append((app, kwargs))

    data_dir = tmp_path / "runtime"
    serve(ServerConfig(data_dir=data_dir), runner=fake_runner)

    assert len(calls) == 1
    app, options = calls[0]
    assert app.title == "Speech Capture Worker API"
    assert options == {
        "host": "127.0.0.1",
        "port": 8765,
        "ssl_certfile": None,
        "ssl_keyfile": None,
        "proxy_headers": False,
        "server_header": False,
        "date_header": False,
        "access_log": False,
    }
    assert (data_dir / "worker.sqlite3").is_file()
    assert (data_dir / "security.sqlite3").is_file()
