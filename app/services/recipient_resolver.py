# services/recipient_resolver.py
class AliasRecipientResolver:
    """PLACEHOLDER: map the alias Postfix delivered to onto the user's real mailbox.

    Return the real address, or None if the alias doesn't exist
    (the API then answers 422 and Postfix bounces the email).
    """

    async def resolve(self, recipient: str) -> str | None:
        raise NotImplementedError   # TODO: look it up in your database or cache