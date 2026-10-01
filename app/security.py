"""Room passwords are salted and never appear in snapshots or logs."""
import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    if not password:
        return ""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}:{digest}"


def password_matches(password: str, encoded: str) -> bool:
    if not encoded:
        return True
    salt, expected = encoded.split(":", 1)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return hmac.compare_digest(expected, actual)
