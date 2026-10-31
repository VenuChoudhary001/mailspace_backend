# services/mime_builder.py
import base64
import os
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from app.services.exceptions import MalformedEmail
from app.models.forward_mail import OutboundAttachment, OutboundEmail   # was: just OutboundEmail


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _one_line(value) -> str:
    """Remove newlines/control characters so a value can never inject extra headers."""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()


def _format_address(name: str, addr: str) -> str:
    name = _one_line(name).replace("\\", "\\\\").replace('"', '\\"')
    addr = _one_line(addr)
    return f'"{name}" <{addr}>' if name else addr


def _addresses(msg, header: str) -> list[tuple[str, str]]:
    values = [str(v) for v in msg.get_all(header, [])]
    return [(n, a) for n, a in getaddresses(values) if a and "@" in a]


def _body_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError):
        raw = part.get_payload(decode=True) or b""
        return raw.decode("utf-8", errors="replace")

def _make_attachment(part, data: bytes, ctype: str, index: int) -> OutboundAttachment:
    # basename() strips any path like "../../x", so filenames can't be abused
    name = os.path.basename(_one_line(part.get_filename() or ""))
    if not name:
        name = f"attachment-{index + 1}" + (".eml" if ctype == "message/rfc822" else "")
    cid = part.get("Content-ID")
    cid = _one_line(cid).strip("<>") if cid else None
    return OutboundAttachment(
        filename=name,
        content_b64=base64.b64encode(data).decode("ascii"),
        content_type=ctype,
        content_id=cid or None,
    )


def _collect_attachments(part, skip: list, out: list[OutboundAttachment]) -> None:
    # A forwarded .eml (or delivery report) is kept as one opaque attachment.
    if part.get_content_maintype() == "message":
        subs = part.get_payload() if part.is_multipart() else []
        data = b"\n".join(s.as_bytes() for s in subs)
        out.append(_make_attachment(part, data, part.get_content_type(), len(out)))
        return
    # Containers (multipart/mixed, related, ...): look inside.
    if part.is_multipart():
        for sub in part.iter_parts():
            _collect_attachments(sub, skip, out)
        return
    # Leaf parts that are the text/html body were already taken: skip them.
    if any(part is s for s in skip):
        return
    data = part.get_payload(decode=True) or b""
    out.append(_make_attachment(part, data, part.get_content_type(), len(out)))

class OutboundEmailBuilder:
    def __init__(
        self,
        from_address: str,                      # on your verified Resend domain
        brand: str = "Mail Forwarder",
        loop_header: str = "X-Forwarded-By-Relay",
    ) -> None:
        self._from_address = from_address
        self._brand = brand
        self._loop_header = loop_header

    def build(self, raw: bytes, destination: str) -> OutboundEmail | None:
        msg = BytesParser(policy=policy.default).parsebytes(raw)

        # Already forwarded by us once? Stop, so we never loop.
        if msg.get(self._loop_header):
            return None

        # Reply-To: keep the original Reply-To; fall back to the original From.
        from_pairs = _addresses(msg, "from")
        reply_pairs = _addresses(msg, "reply-to") or from_pairs
        if not reply_pairs:
            raise MalformedEmail()
        reply_to = [_format_address(n, a) for n, a in reply_pairs]

        # From: always OUR domain. Only the display name comes from the sender.
        origin_name, origin_addr = (from_pairs or reply_pairs)[0]
        display = f"{_one_line(origin_name) or _one_line(origin_addr)} via {self._brand}"

        # Bodies, passed through untouched.
        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        attachments: list[OutboundAttachment] = []
        _collect_attachments(
            msg, [p for p in (text_part, html_part) if p is not None], attachments
        )
        text = _body_text(text_part) if text_part is not None else None
        html = _body_text(html_part) if html_part is not None else None
        if text is None and html is None:
            text = " "  # Resend needs html or text; this covers attachment-only mail

        headers = {self._loop_header: "1"}
        if msg.get("Message-ID"):
            headers["X-Original-Message-ID"] = _one_line(msg["Message-ID"])

        return OutboundEmail(
            sender=_format_address(display, self._from_address),
            to=[destination],
            subject=_one_line(msg.get("Subject", "")),   # unchanged, no "Fwd:" prefix
            reply_to=reply_to,
            text=text,
            html=html,
            headers=headers,
            attachments=attachments,                              # added in step 3b
        )