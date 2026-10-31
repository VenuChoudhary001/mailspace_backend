# services/resend_sender.py
import httpx

from app.core.config import settings
from app.models.forward_mail import OutboundEmail
from app.services.exceptions import PermanentSendError, TransientSendError


RESEND_API_URL = "https://api.resend.com/emails"


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
    """Sends outbound email via the Resend REST API.

    Raises TransientSendError on timeouts / network errors / 429 / 5xx.
    Raises PermanentSendError on any other 4xx.
    Never logs the payload or the response body.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = RESEND_API_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or settings.resend_api_key
        self._base_url = base_url
        self._client = client

    async def send(self, message: OutboundEmail, *, idempotency_key: str) -> None:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=settings.upstream_timeout_seconds)
        try:
            try:
                response = await client.post(
                    self._base_url,
                    headers=headers,
                    json=to_resend_payload(message),
                )
            except httpx.RequestError as exc:   # timeouts, DNS, refused connections, etc.
                raise TransientSendError() from exc

            if response.status_code == 429 or response.status_code >= 500:
                raise TransientSendError()
            if response.status_code >= 400:
                raise PermanentSendError()
            # 2xx: accepted by Resend, nothing to return
        finally:
            if owned_client:
                await client.aclose()
