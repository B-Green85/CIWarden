"""
Self-signed certificate generation for HTTPS mode.

Generates a cert/key pair on first startup using the cryptography library.
Certs are stored in orchestrator/certs/ and reused across restarts.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path

CERTS_DIR = Path(os.path.dirname(__file__)) / "certs"
CERT_FILE = CERTS_DIR / "server.crt"
KEY_FILE = CERTS_DIR / "server.key"


def generate_self_signed_cert(
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> tuple[Path, Path]:
    """Generate a self-signed certificate and private key.

    Returns (cert_path, key_path).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path = cert_path or CERT_FILE
    key_path = key_path or KEY_FILE

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "cdmad-orchestrator"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CDMAD"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    return cert_path, key_path


def ensure_certs(
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> tuple[Path, Path]:
    """Return existing cert/key paths, generating them if missing."""
    cert_path = cert_path or CERT_FILE
    key_path = key_path or KEY_FILE

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    return generate_self_signed_cert(cert_path, key_path)


def is_https_enabled() -> bool:
    """Check if HTTPS mode is active via CDMAD_HTTPS=1."""
    return os.environ.get("CDMAD_HTTPS", "0") == "1"
