# services/signature.py
import hashlib
import hmac

def compute_signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def signature_is_valid(secret: bytes, body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected.encode(), header_value.encode())