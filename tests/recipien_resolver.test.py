# test.py
from __future__ import annotations

import base64
from email.message import EmailMessage
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request

EX_OK = 0
EX_DATAERR = 65
EX_TEMPFAIL = 75
RETRYABLE_4XX = {401, 408, 429}


def build_sample_email(
    sender: str = "Jane Doe <jane.doe@example.com>",
    recipient: str = "hello@mailspace.shop",
    subject: str = "Test forward to alias",
    body_text: str = "Hello from the Mailspace pipe test.",
    body_html: str | None = "<p>Hello from the <strong>Mailspace</strong> pipe test.</p>",
    attachment_bytes: bytes | None = None,
    attachment_filename: str = "note.txt",
) -> bytes:
    """Construct a raw RFC 5322 email byte payload for testing."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = "<test-msg-001@example.com>"
    msg.set_content(body_text)

    if body_html is not None:
        msg.add_alternative(body_html, subtype="html")

    if attachment_bytes is not None:
        msg.add_attachment(
            attachment_bytes,
            maintype="text",
            subtype="plain",
            filename=attachment_filename,
        )

    return msg.as_bytes()


def simulate_postfix_pipe(
    recipient: str,
    raw_email: bytes,
    secret: bytes,
    url: str = "http://127.0.0.1:8000/api/v1/forward-email",
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Mimic postfix_pipe.py against FastAPI and return (postfix_exit_code, response_body)."""
    if not recipient:
        return EX_TEMPFAIL, "missing recipient"
    if not raw_email:
        return EX_DATAERR, "empty raw_email"

    clean_secret = secret.strip()
    body = json.dumps(
        {
            "raw_email": base64.b64encode(raw_email).decode("ascii"),
            "recipient": recipient,
        }
    ).encode("utf-8")

    signature = "sha256=" + hmac.new(clean_secret, body, hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return EX_OK, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if 400 <= exc.code < 500 and exc.code not in RETRYABLE_4XX:
            return EX_DATAERR, f"HTTP {exc.code}: {payload}"
        return EX_TEMPFAIL, f"HTTP {exc.code}: {payload}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return EX_TEMPFAIL, f"connection error: {exc}"


def main() -> None:
    url =  "http://127.0.0.1:8000/api/v1/forward-email"
    recipient = "shopping@mailspace.shop"
    sender =  "Jane Doe <jane.doe@example.com>"

    secret_env = b"jfLWho0HIIiBSfRfcY0KUXEHpqcp6Oab36pkqbU5WcootTIfrtSyZ7lhH17cBIy2C7Sbe9zWfEFWEBKEiTfLHp56dDdXKKX7dIpt9CiOTQtLmFGQ3nTE>"

    secret_file = os.getenv("FORWARD_SECRET_FILE", "/etc/postfix/forward-webhook.secret")

    if secret_env:
        secret = secret_env.encode("utf-8")
    else:
        with open(secret_file, "rb") as fh:
            secret = fh.read().strip()

    raw_email = build_sample_email(
        sender=sender,
        recipient=recipient,
        subject=f"Pipe test for {recipient}",
        body_text=f"Testing forwarding pipeline for {recipient}.",
        attachment_bytes=b"sample attachment payload\n",
    )

    exit_code, output = simulate_postfix_pipe(
        recipient=recipient,
        raw_email=raw_email,
        secret=secret,
        url=url,
    )

    print(f"Postfix exit code: {exit_code}")
    print(f"Backend response:  {output}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()