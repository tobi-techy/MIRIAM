"""Security primitives for Miriam Financial Agent."""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken as FernetInvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import SecurityError


def _derive_fernet_key(secret: str) -> bytes:
    """Derive 32 raw bytes from an arbitrary secret via HKDF-SHA256.

    Single SHA-256 is not a KDF (no salt, fast). HKDF is the standard
    extract-and-expand for turning a high-entropy secret into a key.
    Info is domain-separated so this key cannot collide with other HKDF uses.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"miriam-agent-fernet-v1",
    )
    return hkdf.derive(secret.encode())


def _derive_legacy_key(secret: str) -> bytes:
    """Pre-HKDF derivation (single SHA-256). Kept for decrypting old rows.

    The HKDF migration changed the derived bytes, so ciphertext written
    before the switch can only be opened with this function. New writes
    always use HKDF; this exists solely as a read fallback.
    """
    return hashlib.sha256(secret.encode()).digest()


def create_fernet() -> Fernet:
    """Create a Fernet cipher from the configured encryption key.

    The ENCRYPTION_KEY must be set and stable in production. If unset,
    a persistent key is derived from SECRET_KEY so encrypted data remains
    decryptable across process restarts.
    """
    settings = get_settings()
    key = settings.ENCRYPTION_KEY or settings.SECRET_KEY
    raw = _derive_fernet_key(key)
    return Fernet(create_key_from_bytes(raw))


def create_key_from_bytes(raw: bytes) -> str:
    """Produce a urlsafe-base64 key for Fernet from raw bytes."""

    return base64.urlsafe_b64encode(raw).decode()


def encrypt_value(value: str) -> str:
    """Encrypt a sensitive string for storage."""
    try:
        return create_fernet().encrypt(value.encode()).decode()
    except Exception as e:
        raise SecurityError(f"Encryption failed: {e}")


def _legacy_fernet() -> Fernet:
    """Fernet built with the pre-HKDF SHA-256 derivation (read fallback)."""
    settings = get_settings()
    key = settings.ENCRYPTION_KEY or settings.SECRET_KEY
    return Fernet(create_key_from_bytes(_derive_legacy_key(key)))


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a previously encrypted string.

    Tries the current HKDF key first, then the legacy SHA-256 key so rows
    written before the KDF migration stay readable. A legacy hit is
    re-encrypted on next write; callers do not need to migrate eagerly.
    """
    try:
        return create_fernet().decrypt(ciphertext.encode()).decode()
    except FernetInvalidToken:
        pass
    try:
        return _legacy_fernet().decrypt(ciphertext.encode()).decode()
    except FernetInvalidToken:
        raise SecurityError("Decryption failed: invalid token")
    except Exception as e:
        raise SecurityError(f"Decryption failed: {e}")


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


def hash_password(password: str, salt: bytes) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 (stdlib, no extra deps)."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        iterations=600_000,
    ).hex()


def generate_salt() -> bytes:
    """Generate a random 16-byte password salt."""
    return secrets.token_bytes(16)


def generate_idempotency_key() -> str:
    """Generate a cryptographically random idempotency key."""
    return f"miriam_{secrets.token_urlsafe(24)}"


def mask_pii(value: str, visible: int = 4) -> str:
    """Mask a sensitive identifier, keeping the last `visible` chars."""
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]
