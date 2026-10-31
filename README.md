# Mailspace Backend

Inbound email forwarding service. Postfix receives an email, pipes it to a script that base64-encodes the raw RFC 822 message and POSTs it to the API. The API rebuilds the email so it's sent from a verified domain and forwards it to the real recipient (via Resend), with loop prevention.

## Request flow

```
Postfix pipe → POST /api/v1/forward-email
  → ForwardService.forward()
    → _decode()              base64-decode + size check (<= 25 MB)
    → _forward()
      → resolver.resolve(recipient)   find destination mailbox
      → builder.build(raw, destination) parse + rebuild MIME off the event loop
      → sender.send(outbound)          send via Resend
    → ForwardResult(outcome, reason)
  → map exceptions → HTTP status codes
  → ForwardResponse(status, reason)
```

## Endpoints

### `POST /api/v1/forward-email`

Forwards an inbound email.

**Request body** (`ForwardRequest`):
- `raw_email` (str, min 1) — Base64-encoded RFC 822 message.
- `recipient` (str, 3–320) — Postfix envelope recipient.

**Response** (`ForwardResponse`):
- `status` — `"forwarded"` or `"dropped"`.
- `reason` (str \| null) — optional reason (e.g. `"loop"`).

**Error mapping** (via `_STATUS_BY_ERROR`):
| Error | Status |
|-------|--------|
| `InvalidPayload`, `MalformedEmail`, `UnknownRecipient`, `PermanentSendError` | 422 |
| `MessageTooLarge` | 413 |
| `TransientSendError` | 503 |
| `InternalError` | 500 |

### Health
- `GET /health/live` — process is up.
- `GET /health/ready` — ready to take traffic (dependencies initialised, not shutting down).

## Architecture

- `app/main.py` — FastAPI factory, logging, request-id correlation, concurrency shedding middleware, lifespan (shared `httpx` client + thread pool).
- `app/api/forward_email/main.py` — `POST /forward-email` route; maps service exceptions to HTTP codes; returns `ForwardResponse`.
- `app/services/forward_service.py` — core logic: decode, resolve recipient, build, send; translates unexpected errors into `InternalError`.
- `app/services/mime_builder.py` — `OutboundEmailBuilder`: parses MIME, prevents loops via `X-Forwarded-By-Relay` header, sets `From` to verified domain, preserves `Reply-To`/subject/bodies/attachments.
- `app/services/exceptions.py` — exception hierarchy with `code` attributes used for HTTP detail.
- `app/models/forward_mail.py` — internal dataclasses: `OutboundAttachment`, `OutboundEmail`, `ForwardOutcome`, `ForwardResult`.
- `app/schemas/email.py` — Pydantic API schemas: `ForwardRequest`, `ForwardResponse`.
- `app/core/config.py` — Pydantic `Settings` loaded from `.env`.

## Notes

- `get_forward_service()` in `app/api/forward_email/main.py` currently raises `NotImplementedError` — the `ForwardService` wiring is pending.
- `app/services/email.py` contains a Resend sender stub not yet injected into `ForwardService`.
- Supabase/DB clients in `app/core/db.py` and `app/db/session.py` are placeholders.
