"""Tests for orchestrator.certs — self-signed certificate generation."""
from __future__ import annotations

import os
import ssl
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from orchestrator.certs import ensure_certs, generate_self_signed_cert, is_https_enabled


class TestGenerateSelfSignedCert:
    def test_creates_cert_and_key_files(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"

        result_cert, result_key = generate_self_signed_cert(cert_path, key_path)

        assert result_cert == cert_path
        assert result_key == key_path
        assert cert_path.exists()
        assert key_path.exists()

    def test_cert_is_valid_pem(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        generate_self_signed_cert(cert_path, key_path)

        content = cert_path.read_text()
        assert content.startswith("-----BEGIN CERTIFICATE-----")
        assert "-----END CERTIFICATE-----" in content

    def test_key_is_valid_pem(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        generate_self_signed_cert(cert_path, key_path)

        content = key_path.read_text()
        assert content.startswith("-----BEGIN RSA PRIVATE KEY-----")
        assert "-----END RSA PRIVATE KEY-----" in content

    def test_key_file_has_restricted_permissions(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        generate_self_signed_cert(cert_path, key_path)

        mode = oct(key_path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_cert_loadable_by_ssl_module(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        generate_self_signed_cert(cert_path, key_path)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(str(cert_path), str(key_path))

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "deep" / "nested" / "server.crt"
        key_path = tmp_path / "deep" / "nested" / "server.key"
        generate_self_signed_cert(cert_path, key_path)

        assert cert_path.exists()
        assert key_path.exists()


class TestEnsureCerts:
    def test_generates_when_missing(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"

        result_cert, result_key = ensure_certs(cert_path, key_path)

        assert result_cert.exists()
        assert result_key.exists()

    def test_reuses_existing_certs(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"

        generate_self_signed_cert(cert_path, key_path)
        original_cert_content = cert_path.read_bytes()

        result_cert, _ = ensure_certs(cert_path, key_path)

        assert result_cert.read_bytes() == original_cert_content

    def test_regenerates_when_cert_missing(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"

        # Create only key, not cert
        key_path.write_text("dummy")

        ensure_certs(cert_path, key_path)
        assert cert_path.exists()


class TestIsHttpsEnabled:
    def test_returns_false_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert not is_https_enabled()

    def test_returns_true_when_set(self) -> None:
        with patch.dict(os.environ, {"CDMAD_HTTPS": "1"}):
            assert is_https_enabled()

    def test_returns_false_when_zero(self) -> None:
        with patch.dict(os.environ, {"CDMAD_HTTPS": "0"}):
            assert not is_https_enabled()
