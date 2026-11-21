# services/recipient_resolver.py
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
import asyncpg


@dataclass(frozen=True)
class AliasMapping:
    alias_id: UUID
    user_id: UUID
    domain_id: UUID
    domain_name: str
    local_part: str
    from_name: str | None
    destination_emails: list[str]


class AliasRecipientResolver:
    """Map an incoming alias address to its active, verified destination(s) via Postgres."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def resolve_mapping(self, recipient: str) -> AliasMapping | None:
        normalized = recipient.strip().lower()
        if "@" not in normalized or normalized.count("@") != 1:
            return None

        local_part, domain_name = normalized.split("@", 1)
        if not local_part or not domain_name:
            return None

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    alias_id,
                    user_id,
                    domain_id,
                    domain_name,
                    local_part,
                    from_name,
                    destination_emails
                FROM public.get_alias_mapping($1)
                """,
                normalized,
            )

        if row is None or not row["destination_emails"]:
            return None

        return AliasMapping(
            alias_id=row["alias_id"],
            user_id=row["user_id"],
            domain_id=row["domain_id"],
            domain_name=row["domain_name"],
            local_part=row["local_part"],
            from_name=row["from_name"],
            destination_emails=list(row["destination_emails"]),
        )

    async def resolve(self, recipient: str) -> str | None:
        mapping = await self.resolve_mapping(recipient)
        if mapping is None or not mapping.destination_emails:
            return None
        return mapping.destination_emails[0]