# models/forward_mail.py
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class OutboundAttachment:
    filename: str
    content_b64: str
    content_type: str
    content_id: str | None = None  # set for inline images (cid:)


@dataclass(frozen=True)
class OutboundEmail:
    sender: str            # our verified domain, e.g. "Alice via Brand <fwd@yourdomain.com>"
    to: list[str]          # the user's real mailbox
    subject: str           # unchanged from the original
    reply_to: list[str]    # original Reply-To, or original From if there was none
    text: str | None
    html: str | None
    headers: dict[str, str]
    attachments: list[OutboundAttachment] = field(default_factory=list)


class ForwardOutcome(str, Enum):
    FORWARDED = "forwarded"
    DROPPED = "dropped"


@dataclass(frozen=True)
class ForwardResult:
    outcome: ForwardOutcome
    reason: str | None = None