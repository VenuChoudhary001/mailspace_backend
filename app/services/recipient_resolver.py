# services/recipient_resolver.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID
from supabase import AsyncClient

from app.core import supabase_client


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
    """Map an incoming alias address to its active, verified destination(s) via Supabase RPC."""

    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client = client


    async def resolve_mapping(self, recipient: str) -> AliasMapping | None:
        normalized = recipient.strip().lower()
        if "@" not in normalized or normalized.count("@") != 1:
            return None

        local_part, domain_name = normalized.split("@", 1)
        if not local_part or not domain_name:
            return None

        response = await supabase_client.rpc(
            "get_alias_mapping",
            {"p_recipient": normalized},
        ).execute()

        rows: list[dict[str, Any]] = response.data or []
        if not rows:
            return None

        row = rows[0]
        destinations = row.get("destination_emails") or []
        if not destinations:
            return None

        return AliasMapping(
            alias_id=UUID(str(row["alias_id"])),
            user_id=UUID(str(row["user_id"])),
            domain_id=UUID(str(row["domain_id"])),
            domain_name=str(row["domain_name"]),
            local_part=str(row["local_part"]),
            from_name=row.get("from_name"),
            destination_emails=[str(d) for d in destinations],
        )

    async def resolve(self, recipient: str) -> str | None:
        mapping = await self.resolve_mapping(recipient)
        if mapping is None or not mapping.destination_emails:
            return None
        return mapping.destination_emails[0]