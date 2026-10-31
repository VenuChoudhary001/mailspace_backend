#!/usr/bin/env python3
"""Postfix pipe: read a raw email from stdin and hand it to the FastAPI backend.

master.cf:
    forwarder unix - n n - - pipe
      flags=Rq user=forwarder argv=/opt/forwarder/postfix_pipe.py ${recipient}

(`q` quotes the recipient for safety; `R` adds a Return-Path header.)

Exit codes (sysexits.h, understood by Postfix pipe):
    0   delivered
    65  EX_DATAERR  - permanent failure, Postfix bounces to the sender
    75  EX_TEMPFAIL - temporary failure, Postfix keeps it queued and retries

The message is never written to disk by this script. The shared secret is read
from a root-owned file (pipe commands do not inherit your shell environment):
    install -m 640 -o root -g forwarder /dev/null /etc/postfix/forward-webhook.secret
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request

FASTAPI_URL = os.getenv("FASTAPI_URL", "http://127.0.0.1:8000/api/v1/forward-email")
SECRET_FILE = os.getenv("FORWARD_SECRET_FILE", "/etc/postfix/forward-webhook.secret")

EX_DATAERR = 65
EX_TEMPFAIL = 75

# 4xx responses that are NOT a verdict on the message itself -> retry, don't bounce
RETRYABLE_4XX = {401, 408, 429}


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        sys.stderr.write("postfix_pipe: missing recipient argument\n")
        sys.exit(EX_TEMPFAIL)
    recipient = sys.argv[1]

    raw_email = sys.stdin.buffer.read()
    if not raw_email:
        sys.stderr.write("postfix_pipe: no email data on stdin\n")
        sys.exit(EX_DATAERR)

    try:
        with open(SECRET_FILE, "rb") as fh:
            secret = fh.read().strip()
    except OSError:
        sys.stderr.write("postfix_pipe: cannot read secret file\n")
        sys.exit(EX_TEMPFAIL)

    body = json.dumps(
        {
            "raw_email": base64.b64encode(raw_email).decode("ascii"),
            "recipient": recipient,
        }
    ).encode("utf-8")
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        FASTAPI_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
        method="POST",
    )

    try:
        # Generous timeout: the backend waits on Resend. If this times out,
        # the retry is safe because the backend sends an Idempotency-Key.
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"postfix_pipe: backend returned HTTP {e.code}\n")
        if 400 <= e.code < 500 and e.code not in RETRYABLE_4XX:
            sys.exit(EX_DATAERR)
        sys.exit(EX_TEMPFAIL)
    except (urllib.error.URLError, TimeoutError, OSError):
        sys.stderr.write("postfix_pipe: backend unreachable\n")
        sys.exit(EX_TEMPFAIL)


if __name__ == "__main__":
    main()