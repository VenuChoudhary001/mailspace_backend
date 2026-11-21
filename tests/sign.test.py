import base64, hashlib, hmac, json
import httpx
import uuid
from email.utils import formatdate

# msg_id = f"<{uuid.uuid4()}@example.com>"

SECRET = b"jfLWho0HIIiBSfRfcY0KUXEHpqcp6Oab36pkqbU5WcootTIfrtSyZ7lhH17cBIy2C7Sbe9zWfEFWEBKEiTfLHp56dDdXKKX7dIpt9CiOTQtLmFGQ3nTEggmQR3btqa971yKM3fP15AdzgjDyEkBqAgrtMqqR6Gq0b83M4OHp3hI4fbgaxMOoFuR05KuRjgOB1WPptsevbBPX3XIfDONcJrPdM9AY3WVYEf2aJY70H8EhbXBMc9u48b4V4gdZzrTA"

msg_id = f"<{uuid.uuid4()}@example.com>"

raw_email = f"""From: Alice <alice@example.com>
To: shopping@mailspace.shop
Subject: Recipient Resolution
Reply-To: alice@example.com
Message-ID: {msg_id}
Date: {formatdate(localtime=True)}

Hello, this is a test email. ({msg_id})
"""

raw_b64 = base64.b64encode(raw_email.encode()).decode()
payload = json.dumps({"raw_email": raw_b64, "recipient": "shopping@mailspace.shop"}).encode()
sig = "sha256=" + hmac.new(SECRET, payload, hashlib.sha256).hexdigest()

r = httpx.post(
    "http://localhost:8000/api/v1/forward-email",
    content=payload,
    headers={"Content-Type": "application/json", "X-Signature": sig},
)
print(r.status_code, r.text)