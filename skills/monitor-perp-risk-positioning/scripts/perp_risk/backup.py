"""Consistent SQLite backup with optional authenticated encryption."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .store import RiskStore

_HEADER = b"PERPRISK1"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_ITERATIONS = 600_000


def create_backup(
    store: RiskStore,
    *,
    output: Path,
    passphrase: str | None = None,
) -> Path:
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if passphrase is None:
        return store.backup_to(destination)
    temporary = _temporary_database()
    try:
        store.backup_to(temporary)
        plaintext = temporary.read_bytes()
        salt = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        key = _derive_key(passphrase, salt)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, _HEADER)
        destination.write_bytes(_HEADER + salt + nonce + ciphertext)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup(
    *,
    source: Path,
    destination: Path,
    passphrase: str | None = None,
    force: bool = False,
) -> Path:
    source_path = source.expanduser().resolve()
    target = destination.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"restore destination exists: {target}")
    payload = source_path.read_bytes()
    if payload.startswith(_HEADER):
        if passphrase is None:
            raise ValueError("encrypted backup requires a passphrase")
        start = len(_HEADER)
        salt = payload[start : start + _SALT_BYTES]
        nonce_start = start + _SALT_BYTES
        nonce = payload[nonce_start : nonce_start + _NONCE_BYTES]
        ciphertext = payload[nonce_start + _NONCE_BYTES :]
        key = _derive_key(passphrase, salt)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _HEADER)
    else:
        if passphrase is not None:
            raise ValueError("passphrase supplied for an unencrypted backup")
        plaintext = payload
    if not plaintext.startswith(b"SQLite format 3\x00"):
        raise ValueError("backup does not contain a SQLite database")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".restore.tmp")
    temporary.write_bytes(plaintext)
    connection = sqlite3.connect(temporary)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("restored database failed integrity check")
    finally:
        connection.close()
    temporary.replace(target)
    return target


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 12:
        raise ValueError("backup passphrase must contain at least 12 characters")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    ).derive(passphrase.encode("utf-8"))


def _temporary_database() -> Path:
    handle, name = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    return Path(name)
