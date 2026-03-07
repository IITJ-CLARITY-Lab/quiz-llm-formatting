from __future__ import annotations

import base64
import hashlib
import hmac
import os

from werkzeug.security import check_password_hash, generate_password_hash


PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password cannot be empty.")
    return generate_password_hash(password)


def hash_otp(otp: str) -> str:
    if not otp:
        raise ValueError("OTP cannot be empty.")
    return generate_password_hash(otp)


def verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False

    if not stored_hash.startswith("pbkdf2_sha256$"):
        try:
            return check_password_hash(stored_hash, password)
        except ValueError:
            return False

    try:
        algorithm, iterations_text, salt_b64, digest_b64 = stored_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    salt = base64.b64decode(salt_b64.encode("ascii"))
    expected_digest = base64.b64decode(digest_b64.encode("ascii"))
    test_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
    return hmac.compare_digest(test_digest, expected_digest)


def verify_otp(otp: str, stored_value: str) -> bool:
    if not otp or not stored_value:
        return False

    if stored_value.startswith("pbkdf2_sha256$"):
        return verify_password(otp, stored_value)

    if ":" in stored_value and "$" in stored_value:
        try:
            return check_password_hash(stored_value, otp)
        except ValueError:
            return False

    return hmac.compare_digest(otp, stored_value)
