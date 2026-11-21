# services/recipient_resolver.py
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from uuid import UUID

from supabase import AsyncClient


logger = logging.getLogger(__name__)


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
        print(recipient)
        normalized = recipient.strip().lower()
        if "@" not in normalized or normalized.count("@") != 1:
            logger.debug("Invalid recipient format (missing or multiple '@'): %r", recipient)
            return None

        local_part, domain_name = normalized.split("@", 1)
        if not local_part or not domain_name:
            logger.debug("Invalid recipient format (empty local or domain part): %r", recipient)
            return None

        client = self._client 

        try:
            response = await client.rpc(
                "get_alias_mapping",
                {"p_recipient": normalized},
            ).execute()
            print("Recipient resolver ",response)
        except Exception:
            logger.exception(
                "Failed to execute RPC 'get_alias_mapping' for recipient %s",
                normalized,
            )
            return None

        rows: list[dict[str, Any]] = response.data or []
        if not rows:
            logger.debug("No alias mapping found for recipient %s", normalized)
            return None

        if len(rows) > 1:
            logger.warning(
                "Multiple alias mappings (%d) returned for recipient %s; using first row",
                len(rows),
                normalized,
            )

        row = rows[0]
        destinations = row.get("destination_emails") or []
        if not destinations:
            logger.warning(
                "Alias mapping found for %s (alias_id=%s), but destination_emails is empty",
                normalized,
                row.get("alias_id"),
            )
            return None

        try:
            return AliasMapping(
                alias_id=UUID(str(row["alias_id"])),
                user_id=UUID(str(row["user_id"])),
                domain_id=UUID(str(row["domain_id"])),
                domain_name=str(row["domain_name"]),
                local_part=str(row["local_part"]),
                from_name=row.get("from_name"),
                destination_emails=[str(d) for d in destinations],
            )
        except (KeyError, TypeError, ValueError):
            logger.exception(
                "Malformed alias mapping row returned for recipient %s: %r",
                normalized,
                row,
            )
            return None

    async def resolve(self, recipient: str) -> str | None:
        mapping = await self.resolve_mapping(recipient)
        if mapping is None or not mapping.destination_emails:
            return None
        return mapping.destination_emails[0]