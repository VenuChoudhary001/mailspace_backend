import resend

from app.core.config import settings


def send_email(to: list[str], subject: str, html: str, reply_to: list[str] | None = None) -> str:
    resend.api_key = settings.resend_api_key

    params: resend.Emails.SendParams = {
        "from": settings.email_from,
        "to": to,
        "subject": subject,
        "html": html,
    }
    if reply_to:
        params["reply_to"] = reply_to

    email = resend.Emails.send(params)
    return email["id"]
