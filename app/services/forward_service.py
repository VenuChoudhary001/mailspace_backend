# services/forward_service.py
import asyncio
import base64
import binascii
import hashlib
import logging
from typing import Protocol

from app.models.forward_mail import ForwardOutcome, ForwardResult, OutboundEmail
from app.services.exceptions import (
    ForwardingError, InternalError, InvalidPayload,
    MessageTooLarge, TransientSendError, UnknownRecipient,
)
from app.services.mime_builder import OutboundEmailBuilder

logger = logging.getLogger(__name__)


class RecipientResolver(Protocol):
    async def resolve(self, recipient: str) -> str | None: ...


class EmailSender(Protocol):
    async def send(self, message: OutboundEmail, *, idempotency_key: str) -> None: ...


class ForwardService:
    def __init__(
        self,
        builder: OutboundEmailBuilder,
        resolver: RecipientResolver,
        sender: EmailSender,
        max_raw_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self._builder = builder
        self._resolver = resolver
        self._sender = sender
        self._max_raw_bytes = max_raw_bytes

    async def forward(self, *, raw_email_b64: str, recipient: str) -> ForwardResult:
        raw = self._decode(raw_email_b64)
        ref = hashlib.sha256(raw).hexdigest()[:12]   # the only message id that reaches logs

        try:
            return await self._forward(raw, recipient, ref)
        except TransientSendError as exc:
            logger.warning("deferred ref=%s reason=%s", ref, exc.code)
            raise
        except ForwardingError as exc:
            logger.info("rejected ref=%s reason=%s", ref, exc.code)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("failed ref=%s error=%s", ref, type(exc).__name__)
            raise InternalError() from None         # `from None`: no traceback with email data

    async def _forward(self, raw: bytes, recipient: str, ref: str) -> ForwardResult:
        # destination = await self._resolver.resolve(recipient)
        destination = "venu.choudhary2000@gmail.com"
        if destination is None:
            raise UnknownRecipient()

        # Parsing is CPU-bound and emails can be large: keep it off the event loop.
        outbound = await asyncio.to_thread(self._builder.build, raw, destination)
        if outbound is None:
            logger.warning("dropped ref=%s reason=loop", ref)
            return ForwardResult(ForwardOutcome.DROPPED, reason="loop")

        await self._sender.send(outbound, idempotency_key=self._idempotency_key(raw, destination))
        logger.info("forwarded ref=%s bytes=%d", ref, len(raw))
        return ForwardResult(ForwardOutcome.FORWARDED)

    def _decode(self, raw_email_b64: str) -> bytes:
        try:
            raw = base64.b64decode(raw_email_b64, validate=True)
        except (binascii.Error, ValueError):
            raise InvalidPayload() from None
        if len(raw) > self._max_raw_bytes:
            raise MessageTooLarge()
        return raw

    @staticmethod
    def _idempotency_key(raw: bytes, destination: str) -> str:
        return hashlib.sha256(raw + b"\x00" + destination.encode()).hexdigest()