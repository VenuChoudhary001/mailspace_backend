#!/usr/bin/env python3
"""Postfix pipe: read a raw email from stdin and forward it to the FastAPI
backend as a JSON payload. Non-zero exit tells Postfix delivery failed so it
retries from the queue.

Uses only the Python standard library (urllib) so it runs with the system
python3 — no pip packages required on the mail server.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

# FastAPI webhook URL (override with FASTAPI_URL if the API runs elsewhere)
FASTAPI_URL = os.getenv(
    "FASTAPI_URL", "http://127.0.0.1:8000/api/v1/forward-email"
)


def main():
    # Read raw email message from Postfix stdin
    raw_email = sys.stdin.buffer.read()
    if not raw_email:
        sys.stderr.write("postfix_pipe: no email data on stdin\n")
        sys.exit(1)

    # JSON has no bytes type; base64 keeps the RFC822 message intact
    # regardless of charset/8-bit content.
    payload = {
        "raw_email": base64.b64encode(raw_email).decode("ascii"),
        "size": len(raw_email),
    }

    request = urllib.request.Request(
        FASTAPI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()  # drain body so the connection closes cleanly
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"Error forwarding email to FastAPI: HTTP {e.code}\n")
        sys.exit(1)  # non-zero tells Postfix the delivery failed (queue retry)
    except urllib.error.URLError as e:
        sys.stderr.write(f"Error forwarding email to FastAPI: {e.reason}\n")
        sys.exit(1)  # non-zero tells Postfix the delivery failed (queue retry)


if __name__ == "__main__":
    main()
