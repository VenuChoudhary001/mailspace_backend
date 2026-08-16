import logging
import httpx
from fastapi import HTTPException, status
from app.config import settings

logger = logging.getLogger(__name__)

class CloudflareService:
    
    @staticmethod
    async def create_zone(domain_name: str) -> dict:
        """Step 1: Spin up an isolated zone workspace on Cloudflare's infrastructure."""
        async with httpx.AsyncClient() as client:
            payload = {
                "name": domain_name,
                "account": {"id": settings.CLOUDFLARE_ACCOUNT_ID},
                "jump_start": False,
                "type": "full"
            }
            
            response = await client.post(
                f"{settings.CLOUDFLARE_API_URL}/zones",
                json=payload,
                headers=settings.cloudflare_headers
            )
            
            # Cloudflare might return 200 or 201 on success
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Cloudflare Zone Provisioning Failed: {response.text}"
                )
            
            res_data = response.json()["result"]
            return {
                "zone_id": res_data["id"],
                "name_servers": res_data["name_servers"],
                "status": res_data["status"]
            }

    @staticmethod
    async def get_zone_activation_status(zone_id: str) -> dict:
        """
        Queries Cloudflare to read live zone activation metadata.
        Returns the raw state ('active', 'pending', etc.) and configuration strings.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.CLOUDFLARE_API_URL}/zones/{zone_id}",
                headers=settings.cloudflare_headers
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Cloudflare infrastructure status query failed: {response.text}"
                )
                
            result = response.json().get("result", {})
            return {
                "status": result.get("status"), # e.g., 'active', 'pending', 'moved'
                "name_servers": result.get("name_servers", []),
                "activated_on": result.get("activated_on") # None if pending, ISO timestamp if active
            }
        
    @staticmethod
    async def configure_dns_records(zone_id: str) -> None:
        """Step 2: Programmatically attach the high-reputation routing MX records."""
        mx_records = [
            {"type": "MX", "name": "@", "content": "route1.mx.cloudflare.net", "priority": 10, "ttl": 3600},
            {"type": "MX", "name": "@", "content": "route2.mx.cloudflare.net", "priority": 20, "ttl": 3600},
            {"type": "MX", "name": "@", "content": "route3.mx.cloudflare.net", "priority": 30, "ttl": 3600},
            {"type": "TXT", "name": "@", "content": "v=spf1 include:_spf.mx.cloudflare.net ~all", "ttl": 3600}
        ]
        
        # Open ONE client connection pool for all 4 requests
        async with httpx.AsyncClient() as client:
            for record in mx_records:
                res = await client.post(
                    f"{settings.CLOUDFLARE_API_URL}/zones/{zone_id}/dns_records",
                    json=record,
                    headers=settings.cloudflare_headers
                )
                if not res.is_success:
                    # Cloudflare error 81058 = "An identical record already exists."
                    # This happens on retries (client resend, recovery after a prior
                    # partial failure, etc.) — the desired end state (record present)
                    # is already achieved, so treat it as a no-op rather than an error.
                    error_codes = {err.get("code") for err in res.json().get("errors", [])}
                    if 81058 in error_codes:
                        logger.info(f"DNS record {record['type']} already present on zone {zone_id}, skipping.")
                        continue

                    logger.error(f"DNS Injection failed for record {record['type']}: {res.text}")
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Failed setting critical MX/SPF records: {res.text}"
                    )

    @staticmethod
    async def verify_destination_address(email: str) -> dict:
        """Step 3: Register a target destination email at the account layer."""
        async with httpx.AsyncClient() as client:
            payload = {"email": email} 
            
            response = await client.post(
                f"{settings.CLOUDFLARE_API_URL}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses",
                json=payload,
                headers=settings.cloudflare_headers
            )
            
            # Handle the 409 Conflict scenario dynamically
            if response.status_code == 409:
                return {
                    "status": "already_registered",
                    "message": f"The email {email} has already been registered on this account previously."
                }
                
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Destination address registration failed: {response.text}"
                )
                
            return {
                "status": "verification_dispatched",
                "message": f"Verification email dispatched to {email}. Please click verify link."
            }

    @staticmethod
    async def get_destination_address_status(email: str) -> dict:
        """
        Step 3.5: Fetch the current verification status for a destination email.
        Returns whether the address is verified, pending, or not found.
        """
        async with httpx.AsyncClient() as client:
            # We use a query parameter to filter directly for the email address
            params = {"direction": "asc", "verified": None} 
            
            response = await client.get(
                f"{settings.CLOUDFLARE_API_URL}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses",
                headers=settings.cloudflare_headers,
                params=params
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to fetch destination address status: {response.text}"
                )
            
            # Search through the result list for our specific email
            addresses = response.json().get("result", [])
            target_record = next((addr for addr in addresses if addr["email"].lower() == email.lower()), None)
            
            if not target_record:
                return {
                    "status": "not_found",
                    "verified": False,
                    "message": f"The email {email} is not registered on this account."
                }
            
            # Cloudflare populates the 'verified' field with a timestamp string once verified.
            # If it is unverified, 'verified' is typically null or empty.
            is_verified = target_record.get("verified") is not None
            
            return {
                "status": "verified" if is_verified else "pending",
                "verified": is_verified,
                "created_at": target_record.get("created"),
                "verified_at": target_record.get("verified")  # Will be a timestamp string or None
            }

    @staticmethod
    async def create_forwarding_rule(zone_id: str, domain_name: str, prefix: str, destination: str) -> dict:
        """Step 4: Create a live routing rule forwarding alias -> destination."""
        async with httpx.AsyncClient() as client:
            payload = {
                "actions": [{"type": "forward", "value": [destination]}],
                "matchers": [{"field": "to", "type": "literal", "value": f"{prefix}@{domain_name}"}],
                "name": f"Mailspace Alias: {prefix}",
                "enabled": True
            }
            
            response = await client.post(
                f"{settings.CLOUDFLARE_API_URL}/zones/{zone_id}/email/routing/rules",
                json=payload,
                headers=settings.cloudflare_headers
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to generate email routing rules: {response.text}"
                )
                
            return response.json()["result"]
    
    @staticmethod
    async def delete_forwarding_rule(zone_id: str, rule_id: str) -> None:
        """
        Permanently deletes a specific email routing alias rule from Cloudflare infrastructure.
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{settings.CLOUDFLARE_API_URL}/zones/{zone_id}/email/routing/rules/{rule_id}",
                headers=settings.cloudflare_headers
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Cloudflare rejected routing rule deletion: {response.text}"
                )
    
    @staticmethod
    async def delete_zone(zone_id: str) -> None:
        """
        Permanently deletes a zone (domain) from Cloudflare infrastructure.
        Cancels all underlying routing instantly.
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{settings.CLOUDFLARE_API_URL}/zones/{zone_id}",
                headers=settings.cloudflare_headers
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Cloudflare rejected zone infrastructure tear-down: {response.text}"
                )
    
    @staticmethod
    async def delete_destination_address(destination_id: str) -> None:
        """
        Permanently deletes a registered destination(email) address from the Cloudflare account.
        Any routing rules pointing to this address will be automatically disabled by Cloudflare.
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{settings.CLOUDFLARE_API_URL}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses/{destination_id}",
                headers=settings.cloudflare_headers
            )
            
            if not response.is_success:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Cloudflare rejected destination inbox deletion: {response.text}"
                )