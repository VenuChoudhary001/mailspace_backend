# schema/forward_mail.py
from typing import Literal

from pydantic import BaseModel, Field


class ForwardRequest(BaseModel):
    raw_email: str = Field(min_length=1, description="Base64-encoded RFC 822 message")
    recipient: str = Field(min_length=3, max_length=320, description="Postfix envelope recipient")


class ForwardResponse(BaseModel):
    status: Literal["forwarded", "dropped"]
    reason: str | None = None