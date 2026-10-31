# services/resend_sender.py
from app.models.forward_mail import OutboundEmail


def to_resend_payload(message: OutboundEmail) -> dict:
    """Body for POST https://api.resend.com/emails"""
    payload: dict = {
        "from": message.sender,
        "to": message.to,
        "subject": message.subject,
        "reply_to": message.reply_to,      # the original sender, so replies reach them
        "headers": message.headers,
    }
    if message.html is not None:
        payload["html"] = message.html
    if message.text is not None:
        payload["text"] = message.text
    if message.attachments:
        payload["attachments"] = [
            {
                "filename": a.filename,
                "content": a.content_b64,
                "content_type": a.content_type,
                **({"content_id": a.content_id} if a.content_id else {}),
            }
            for a in message.attachments
        ]
    return payload


class ResendEmailSender:
    """PLACEHOLDER for the actual Resend call.

    When you implement it:
      * POST to Resend with to_resend_payload(message), sending your API key
        and an `Idempotency-Key: <idempotency_key>` header.
      * Raise TransientSendError on timeouts / network errors / 429 / 5xx.
      * Raise PermanentSendError on any other 4xx.
      * Never log the payload or the response body.
    """

    async def send(self, message: OutboundEmail, *, idempotency_key: str) -> None:
        raise NotImplementedError