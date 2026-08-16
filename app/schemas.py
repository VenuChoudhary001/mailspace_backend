# app/schemas.py
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime
from typing import List
from app.database.models import DomainStatus, DestinationStatus

# Compile regex strings once for easy maintenance
DOMAIN_REGEX = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
PREFIX_REGEX = r"^[a-z0-9._+-]+$"

class DomainOnboardRequest(BaseModel):
    # Enforce standard maximum domain length (253 chars) and strict formatting
    domain_name: str = Field(
        ..., 
        max_length=253,
        pattern=DOMAIN_REGEX,
        examples=["mailspace.shop"]
    )

class DestinationEmailRequest(BaseModel):
    email: EmailStr = Field(..., examples=["yourpersonalinbox@gmail.com"])

class ForwardingRuleRequest(BaseModel):
    # Enforce strict email local-part prefix formatting and a standard max length
    alias_prefix: str = Field(
        ..., 
        max_length=64,
        pattern=PREFIX_REGEX,
        examples=["hello"]
    )  # e.g., for hello@yourdomain.com
    destination_email: EmailStr = Field(..., examples=["yourpersonalinbox@gmail.com"])


class AliasResponse(BaseModel):
    id: str
    alias_name: str
    full_email_address: str # Constructed field (e.g., support@domain.com)
    destination_email: str
    is_active: bool
    forward_count: int
    created_at: datetime

    class Config:
        from_attributes = True

class DomainDetailResponse(BaseModel):
    id: str
    name: str
    status: DomainStatus
    created_at: datetime
    aliases: List[AliasResponse]

    class Config:
        from_attributes = True

class DestinationResponse(BaseModel):
    id: str
    email: str
    status: DestinationStatus
    created_at: datetime

    class Config:
        from_attributes = True

class UserDashboardResponse(BaseModel):
    user_id: str
    is_subscribed: bool
    created_at: datetime
    domains: List[DomainDetailResponse]
    destinations: List[DestinationResponse]
    
    class Config:
        from_attributes = True