"""The phone connection's own certificate: made once, then kept, because every paired phone
trusts exactly this one by its fingerprint. It lives in the data folder, so updates leave it alone.

cryptography is optional (below `# optional` in requirements.txt), so it is imported only in the
functions: `ensure_certificate` raises ImportError without it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import AppPaths

if TYPE_CHECKING:
    from cryptography import x509

log = logging.getLogger(__name__)

_CERT_NAME = "certificate.pem"
_KEY_NAME = "key.pem"
_LIFETIME = timedelta(days=20 * 365 + 5)  # 20 years, leap days included
_CLOCK_MARGIN = timedelta(days=1)  # a phone whose clock runs behind still accepts it


def ensure_certificate(paths: AppPaths) -> tuple[Path, Path, str]:
    """(certificate file, key file, SHA-256 fingerprint in lowercase hex), made if there's none."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    folder = paths.root / "phone"
    cert = folder / _CERT_NAME
    key = folder / _KEY_NAME
    if cert.exists() and key.exists():
        try:
            return cert, key, _fingerprint(x509.load_pem_x509_certificate(cert.read_bytes()))
        except ValueError:
            log.warning("The phone connection's certificate is damaged; making a new one")
    folder.mkdir(parents=True, exist_ok=True)
    cert.unlink(missing_ok=True)  # never an old certificate beside a new key
    made = _make(key)
    _write(cert, made.public_bytes(serialization.Encoding.PEM))
    log.info("Made the phone connection's certificate")
    return cert, key, _fingerprint(made)


def _make(key_file: Path) -> x509.Certificate:
    """A new key, saved in `key_file`, and its self-signed certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    _write(
        key_file,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AirGrabber")])
    start = datetime.now(UTC) - _CLOCK_MARGIN
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + _LIFETIME)
        .sign(key, hashes.SHA256())
    )


def _write(path: Path, data: bytes) -> None:
    """Whole or not at all: a crash halfway leaves no half-written file behind."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _fingerprint(cert: x509.Certificate) -> str:
    from cryptography.hazmat.primitives import serialization

    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
