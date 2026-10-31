# services/exceptions.py
class MalformedEmail(Exception):
    """The email has neither a From nor a Reply-To, so there is nobody to reply to."""
    
class ForwardingError(Exception):
    code = "forwarding_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class InvalidPayload(ForwardingError):
    code = "invalid_payload"


class MessageTooLarge(ForwardingError):
    code = "too_large"


class UnknownRecipient(ForwardingError):
    code = "unknown_recipient"


class MalformedEmail(ForwardingError):
    code = "malformed"


class TransientSendError(ForwardingError):   # timeout / 429 / 5xx -> Postfix should retry
    code = "transient"


class PermanentSendError(ForwardingError):   # other 4xx from Resend -> don't retry
    code = "send_rejected"


class InternalError(ForwardingError):        # anything unexpected
    code = "internal"