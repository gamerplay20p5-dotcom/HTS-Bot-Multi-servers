"""Protege senhas opcionais enquanto a candidatura aguarda revisao."""

from __future__ import annotations

from .i18n import t

import base64
import hashlib

from cryptography.fernet import Fernet


def _cipher(secret: str) -> Fernet:
    if not secret:
        raise ValueError(t('DISCORD_TOKEN ou WL_ENCRYPTION_KEY necessario para proteger senhas pendentes.'))
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_password(password: str, secret: str) -> str:
    return _cipher(secret).encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(ciphertext: str, secret: str) -> str:
    return _cipher(secret).decrypt(ciphertext.encode("ascii")).decode("utf-8")
