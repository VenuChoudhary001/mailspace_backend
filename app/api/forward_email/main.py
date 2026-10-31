# api/forward_mail/main.py
from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from pydantic_core import ValidationError
from functools import lru_cache
from app.schemas.email import ForwardRequest, ForwardResponse
from app.services.exceptions import (
    ForwardingError, InternalError, InvalidPayload, MalformedEmail,
    MessageTooLarge, PermanentSendError, TransientSendError, UnknownRecipient,
)
from app.services.forward_service import ForwardService
from app.services.signature import signature_is_valid
from app.core.config import settings

from app.services.mime_builder import OutboundEmailBuilder
from app.services.recipient_resolver import AliasRecipientResolver
from app.services.resend import ResendEmailSender

router = APIRouter(tags=["forwarding"])

@lru_cache
def get_recipient_resolver() -> AliasRecipientResolver:
    return AliasRecipientResolver()


@lru_cache
def get_email_sender() -> ResendEmailSender:
    return ResendEmailSender()

def get_forward_service(
    resolver: AliasRecipientResolver = Depends(get_recipient_resolver),
    sender: ResendEmailSender = Depends(get_email_sender),
) -> ForwardService:
    return ForwardService(
        builder=OutboundEmailBuilder(
            from_address=settings.forward_from_address,
            brand=settings.forward_brand,
        ),
        resolver=resolver,
        sender=sender,
        max_raw_bytes=settings.max_raw_bytes,
    )



_STATUS_BY_ERROR = {
    InvalidPayload: status.HTTP_422_UNPROCESSABLE_ENTITY,
    MalformedEmail: status.HTTP_422_UNPROCESSABLE_ENTITY,
    UnknownRecipient: status.HTTP_422_UNPROCESSABLE_ENTITY,
    PermanentSendError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    MessageTooLarge: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    TransientSendError: status.HTTP_503_SERVICE_UNAVAILABLE,
    InternalError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}

async def read_verified_body(
    request: Request,
    x_signature: str | None = Header(default=None),
) -> bytes:
    limit = settings.max_request_bytes
    chunks, total = [], 0
    async for chunk in request.stream():          # read with a hard size cap
        total += len(chunk)
        if total > limit:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="too_large")
        chunks.append(chunk)
    body = b"".join(chunks)

    if not signature_is_valid(settings.webhook_secret, body, x_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bad_signature")
    return body


async def get_forward_request(body: bytes = Depends(read_verified_body)) -> ForwardRequest:
    try:
        return ForwardRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_payload") from None

    
@router.post("/forward-email", response_model=ForwardResponse)
async def forward_email(
    payload: ForwardRequest = Depends(get_forward_request),
    service: ForwardService = Depends(get_forward_service),
) -> ForwardResponse:
    try:
        result = await service.forward(
            raw_email_b64=payload.raw_email,
            recipient=payload.recipient,
        )
    except ForwardingError as exc:
        raise HTTPException(
            status_code=_STATUS_BY_ERROR.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR),
            detail=exc.code,
        ) from None
    return ForwardResponse(status=result.outcome.value, reason=result.reason)