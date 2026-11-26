"""Password (argon2id), token di sessione e CSRF."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()   # parametri predefiniti di argon2-cffi (argon2id, RFC 9106 profilo basso)
# hash di una password a caso: chi prova un'email inesistente aspetta quanto chi la indovina
_HASH_FINTO = _hasher.hash(secrets.token_urlsafe(16))
PASSWORD_MIN = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verifica_password(hash_salvato: str | None, password: str) -> bool:
    try:
        return _hasher.verify(hash_salvato or _HASH_FINTO, password) and hash_salvato is not None
    except (VerificationError, InvalidHashError):
        return False


def da_aggiornare(hash_salvato: str) -> bool:
    return _hasher.check_needs_rehash(hash_salvato)


def nuovo_token() -> str:
    return secrets.token_urlsafe(32)


def impronta(token: str) -> str:
    """Nel database si salva solo l'impronta del token del cookie."""
    return hashlib.sha256(token.encode()).hexdigest()


def stesso_token(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
