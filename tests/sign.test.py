import base64, hashlib, hmac, json
import httpx

SECRET = b"jfLWho0HIIiBSfRfcY0KUXEHpqcp6Oab36pkqbU5WcootTIfrtSyZ7lhH17cBIy2C7Sbe9zWfEFWEBKEiTfLHp56dDdXKKX7dIpt9CiOTQtLmFGQ3nTEggmQR3btqa971yKM3fP15AdzgjDyEkBqAgrtMqqR6Gq0b83M4OHp3hI4fbgaxMOoFuR05KuRjgOB1WPptsevbBPX3XIfDONcJrPdM9AY3WVYEf2aJY70H8EhbXBMc9u48b4V4gdZzrTA"

raw_email = """From: Alice <alice@example.com>
To: user@yourdomain.com
Subject: Test forward
Reply-To: alice@example.com

Hello, this is a test email.
"""

raw_b64 = base64.b64encode(raw_email.encode()).decode()
payload = json.dumps({"raw_email": raw_b64, "recipient": "user@yourdomain.com"}).encode()
sig = "sha256=" + hmac.new(SECRET, payload, hashlib.sha256).hexdigest()

r = httpx.post(
    "http://localhost:8000/api/v1/forward-email",
    content=payload,
    headers={"Content-Type": "application/json", "X-Signature": sig},
)
print(r.status_code, r.text)