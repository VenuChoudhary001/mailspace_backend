#!/usr/bin/env python3
import sys
import httpx

# FastAPI webhook URL (adjust port if it's 8000 instead of 800)
FASTAPI_URL = "http://127.0.0.1:800/api/v1/inbound-email"

def main():
    # Read raw email message from Postfix stdin
    raw_email = sys.stdin.buffer.read()
    
    try:
        # Send raw email bytes to FastAPI
        response = httpx.post(
            FASTAPI_URL,
            content=raw_email,
            headers={"Content-Type": "message/rfc822"},
            timeout=15.0
        )
        response.raise_for_status()
    except Exception as e:
        sys.stderr.write(f"Error forwarding email to FastAPI: {e}\n")
        sys.exit(1) # Returning non-zero tells Postfix the delivery failed (triggers queue retry)

if __name__ == "__main__":
    main()
