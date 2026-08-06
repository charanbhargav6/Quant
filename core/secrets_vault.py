"""
CRAVE — Secrets Vault
=======================
FIXES: passwords/API secrets were stored as plaintext columns in the
accounts DB table and written raw into .env.<profile> files. Anyone with
filesystem or DB read access (backups, a synced State/ repo, a leaked
sqlite file) gets live broker credentials in the clear.

This wraps Fernet symmetric encryption around any secret before it's
persisted. The key itself must NOT live in the repo or the DB — it goes
in the environment (ACCOUNT_ENCRYPTION_KEY), ideally injected by whatever
secrets manager/host you deploy on, not committed to .env.

Generate a key once:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
import logging

logger = logging.getLogger("crave.secrets_vault")

_KEY_ENV_VAR = "ACCOUNT_ENCRYPTION_KEY"
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise RuntimeError(
            "cryptography package not installed. Run: pip install cryptography"
        )
    key = os.environ.get(_KEY_ENV_VAR)
    if not key:
        raise RuntimeError(
            f"{_KEY_ENV_VAR} not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and put it in your "
            "environment — never commit it to the repo."
        )
    _fernet = Fernet(key.encode())
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
