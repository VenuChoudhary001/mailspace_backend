import logging
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, status, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from httpx import HTTPStatusError, RequestError

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload

# Project Imports
from app.schemas import (
    DomainOnboardRequest, 
    DestinationEmailRequest, 
    ForwardingRuleRequest, 
    UserDashboardResponse, 
    DomainDetailResponse, 
    AliasResponse,
    DestinationResponse
)
from app.services.cloudflare import CloudflareService
from app.services.auth import get_current_user, UserSession
from app.config import settings
from app.database.config import engine, get_db_session
from app.database.models import Base, User, Domain, Alias, Destination, DomainStatus, DestinationStatus 
from app.rateLimiter import infra_onboard_limiter, infra_rule_limiter


# 1. Lifespan Event Handler (schema is managed by Alembic; run `alembic upgrade head` as a deploy step)
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Clean up the connection pool on shutdown
    # await engine.dispose()


app = FastAPI(
    title="Mailspace Engine", 
    description="Automated Email Routing & Workspace Core Layer",
    lifespan=lifespan
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)


@app.get("/health")
async def health_check():
    """Lightweight liveness check for the hosting platform (e.g. Render). Deliberately DB-independent."""
    return {"status": "ok"}


@app.post("/api/v1/domains/onboard", status_code=status.HTTP_201_CREATED)
async def onboard_custom_domain(
    payload: DomainOnboardRequest,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(infra_onboard_limiter)
):
    """
    Kicks off automated domain connection. Provisions the workspace zone, 
    injects required MX routing specs, and returns the verification name servers.
    """
    clean_domain = payload.domain_name.strip().lower()

    # 1. Fetch or bootstrap the user entity
    db_user = await db.get(User, user.user_id)
    if not db_user:
        db_user = User(id=user.user_id)
        db.add(db_user)
        try:
            await db.flush() 
        except IntegrityError:
            await db.rollback()
            db_user = await db.get(User, user.user_id)

    # 2. Local DB Check (Fail fast before hitting external APIs)
    existing_domain_stmt = select(Domain).where(Domain.name == clean_domain)
    existing_domain_res = await db.execute(existing_domain_stmt)
    if existing_domain_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="This domain has already been registered in our system."
        )
    
    # 3. Interact with Cloudflare safely
    zone_id = None
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            zone_id = f"mock_zone_{uuid.uuid4().hex[:10]}"
            zone_info = {
                "zone_id": zone_id,
                "name_servers": ["mock-ns1.cloudflare.com", "mock-ns2.cloudflare.com"]
            }
            logger.info(f"[SANDBOX MODE] Mocking Cloudflare setup for {clean_domain}")
        else:
            zone_info = await CloudflareService.create_zone(clean_domain)
            zone_id = zone_info["zone_id"]
            await CloudflareService.configure_dns_records(zone_id)
        
    except Exception as cf_err:
        logger.error(f"Cloudflare provisioning failed for {clean_domain}: {str(cf_err)}")
        if zone_id and not settings.CLOUDFLARE_SANDBOX_MODE:
            try:
                await CloudflareService.delete_zone(zone_id)
            except Exception as cleanup_err:
                logger.critical(f"Failed to cleanup orphaned zone {zone_id}: {str(cleanup_err)}")
                
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to provision domain infrastructure with provider. Please try again."
        )
    
    # 4. Commit to Database using non-ambiguous mappings
    try:
        new_domain = Domain(
            id=zone_id,
            name=clean_domain,
            status=DomainStatus.PENDING,
            owner_id=user.user_id
        )
        db.add(new_domain)
        await db.commit()

    except IntegrityError:
        await db.rollback()
        logger.error(f"Database conflict during onboarding for {clean_domain}. Rolling back Cloudflare.")
        if not settings.CLOUDFLARE_SANDBOX_MODE:
            try:
                await CloudflareService.delete_zone(zone_id)
            except Exception as cleanup_err:
                logger.critical(f"Orphaned Cloudflare zone leak! Zone ID: {zone_id}. Error: {str(cleanup_err)}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This domain was registered concurrently. Please try again."
        )
    except Exception as db_err:
        await db.rollback()
        logger.error(f"Unexpected database error: {str(db_err)}")
        if zone_id and not settings.CLOUDFLARE_SANDBOX_MODE:
            try:
                await CloudflareService.delete_zone(zone_id)
            except Exception:
                logger.critical(f"Orphaned Cloudflare zone leak! Zone ID: {zone_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected database error occurred."
        )

    return {
        "status": "pending_dns_activation",
        "domain": clean_domain,
        "cloudflare_zone_id": zone_id,
        "instructions": "Point your domain registrar's nameservers to the values below.",
        "required_nameservers": zone_info["name_servers"]
    }

@app.get("/api/v1/domains/{zone_id}/sync")
async def sync_domain_dns_status(
    zone_id: str,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Checks if nameserver configuration is updated globally.
    If active, updates local DB cache parameters.
    """
    # 1. Fetch local record with scope verification
    domain = await db.get(Domain, zone_id)
    if not domain or domain.owner_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Domain configuration not found under your account profile."
        )

    # 2. Short-circuit if already verified locally
    if domain.status == DomainStatus.ACTIVE:
        return {
            "status": "active",
            "is_active": True,
            "message": "Domain is fully active and processing emails."
        }

    # 3. Pull truth from infrastructure provider
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            cf_data = {"status": "active"}
            logger.info(f"[SANDBOX MODE] Simulating successful active status sync for {domain.name}")
        else:
            cf_data = await CloudflareService.get_zone_activation_status(zone_id)
            
    except Exception as err:
        logger.error(f"DNS tracking synchronization error for zone {zone_id}: {str(err)}")
        raise HTTPException(status_code=502, detail="Upstream lookup failed.")

    # 4. Handle State Promotion
    if cf_data.get("status") == "active":
        domain.status = DomainStatus.ACTIVE
        
        # ⚠️ PRO-TIP ARCHITECTURE HOOK:
        # Now that the domain is officially active on Cloudflare, you can optionally
        # run your `CloudflareService.enable_email_routing(zone_id)` helper function 
        # right here to make sure email intercepting wakes up automatically.
        
        await db.commit()

    return {
        "status": domain.status, # Returns "pending" or "active"
        "is_active": domain.status == DomainStatus.ACTIVE,
        "assigned_nameservers": cf_data.get("name_servers", [])
    }

@app.post("/api/v1/destinations/register")
async def register_destination_inbox(
    payload: DestinationEmailRequest,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(infra_onboard_limiter)
):
    """
    Registers a target inbox account-wide, persists it locally, and flags 
    Cloudflare to fire a validation token link directly to that inbox.
    """
    clean_email = payload.email.strip().lower()
    cf_id = None

    # 1. Dispatch to Upstream Mail Router Infrastructure
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            cf_id = f"mock_dest_{uuid.uuid4().hex[:10]}"
            cf_response = {"status": "verification_dispatched"}
            logger.info(f"[SANDBOX MODE] Mocking destination registration for {clean_email}")
        else:
            cf_response = await CloudflareService.verify_destination_address(clean_email)
            if cf_response.get("status") == "already_registered":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This email address is already registered on our infrastructure."
                )
            cf_id = cf_response.get("id") or clean_email

    except HTTPException:
        raise
    except HTTPStatusError as http_err:
        status_code = http_err.response.status_code
        cf_error_detail = http_err.response.json().get("errors", [{}])[0].get("message", "Unknown error")
        logger.error(f"Cloudflare returned error {status_code} for {clean_email}: {cf_error_detail}")
        if status_code == 409:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Destination email already tracked.")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Provider rejected infrastructure payload.")
    except Exception as general_err:
        logger.critical(f"Downstream transport connection anomaly: {str(general_err)}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Infrastructure tier unreachable.")

    # 2. Local Database Synchronization
    try:
        new_destination = Destination(
            id=cf_id,
            email=clean_email,
            status=DestinationStatus.PENDING,
            user_id=user.user_id
        )
        db.add(new_destination)
        await db.commit()

    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already registered this destination address."
        )
    except Exception as db_err:
        await db.rollback()
        logger.critical(f"Database write fault on destination entry: {str(db_err)}")
        # Roll back the upstream Cloudflare registration so we don't leak an orphaned
        # destination address that the user has no local record of.
        if not settings.CLOUDFLARE_SANDBOX_MODE:
            try:
                await CloudflareService.delete_destination_address(cf_id)
            except Exception as cleanup_err:
                logger.critical(f"Orphaned Cloudflare destination leak! ID: {cf_id}. Error: {str(cleanup_err)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Persistence mapping failed.")

    return {
        "status": "verification_pending",
        "email": clean_email,
        "destination_id": cf_id,
        "message": f"Verification email successfully dispatched to {clean_email}. Please check your inbox."
    }

@app.get("/api/v1/destinations/sync")
async def sync_destination_status_by_email(
    email: str,  # 👈 Frontend just passes ?email=venuchoudhary24@gmail.com
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Queries Cloudflare to check if the email verification link was clicked,
    using the raw email string to locate records.
    """
    clean_email = email.strip().lower()

    # 1. Look up the destination record using the email string and user scope
    stmt = select(Destination).where(
        Destination.email == clean_email,
        Destination.user_id == user.user_id
    )
    res = await db.execute(stmt)
    dest = res.scalar_one_or_none()
    
    if not dest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This email address is not registered under your profile."
        )

    # 2. Optimization check
    if dest.status == DestinationStatus.VERIFIED:
        return {"status": "verified", "verified": True, "email": dest.email}

    # 3. Fetch status from Cloudflare / Sandbox
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            cf_data = {"status": "verified", "verified": True, "verified_at": "2026-07-02T12:00:00Z"}
        else:
            cf_data = await CloudflareService.get_destination_address_status(clean_email)
    except Exception as err:
        logger.error(f"Status query exception for {clean_email}: {str(err)}")
        raise HTTPException(status_code=502, detail="Upstream provider unreachable.")

    # 4. State Sync promotion
    if cf_data.get("verified") is True:
        dest.status = DestinationStatus.VERIFIED
        await db.commit()

    return {
        "status": dest.status,
        "verified": dest.status == DestinationStatus.VERIFIED,
        "email": dest.email
    }

@app.post("/api/v1/rules/create")
async def create_routing_rule(
    zone_id: str, 
    payload: ForwardingRuleRequest,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(infra_rule_limiter)
):
    """
    Binds an alias prefix to an authorized destination inbox reference token.
    """
    clean_prefix = payload.alias_prefix.strip().lower()
    target_email = payload.destination_email.strip().lower()

    # 1. Authorize Domain Ownership Safely
    domain = await db.get(Domain, zone_id)
    if not domain or domain.owner_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Access Denied: You do not own this domain resource."
        )
    
    # 2. Validate Target Destination Ownership/Verification status locally
    dest_stmt = select(Destination).where(
        Destination.email == target_email, 
        Destination.user_id == user.user_id
    )
    dest_res = await db.execute(dest_stmt)
    destination_record = dest_res.scalar_one_or_none()
    
    if not destination_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target destination email must be registered and verified within your settings profile first."
        )

    # 3. Check for Existing Target Prefix Collisions
    alias_stmt = select(Alias).where(Alias.domain_id == zone_id, Alias.alias_name == clean_prefix)
    alias_res = await db.execute(alias_stmt)
    if alias_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=f"The alias prefix '{clean_prefix}' already exists for this domain."
        )
    
    # 4. Initialize Cloudflare Routing Engine Architecture
    try:
        relay_destination = f"{clean_prefix}+{domain.name}@{settings.RELAY_DOMAIN}"

        if settings.CLOUDFLARE_SANDBOX_MODE:
            rule_id = f"mock_rule_{uuid.uuid4().hex[:10]}"
        else:
            rule = await CloudflareService.create_forwarding_rule(
                zone_id=zone_id,
                domain_name=domain.name,
                prefix=clean_prefix,
                destination=relay_destination,   # ← changed from target_email
            )
            rule_id = rule["id"]
    except Exception as cf_err:
        logger.error(f"Routing rule injection engine failure: {str(cf_err)}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream configuration payload rejected.")

    # 5. Atomic DB Insertion mapping back to the proper Destination ID
    try:
        new_alias = Alias(
            id=rule_id,
            alias_name=clean_prefix,
            domain_id=zone_id,
            destination_id=destination_record.id  # Clean SQL Relationship linkage
        )
        db.add(new_alias)
        await db.commit()
    except Exception as db_err:
        await db.rollback()
        logger.critical(f"Database link mapping failure: {str(db_err)}")
        # Roll back the upstream Cloudflare rule so we don't leave a live forwarding
        # rule with no corresponding local record (and no way for the user to manage it).
        if not settings.CLOUDFLARE_SANDBOX_MODE:
            try:
                await CloudflareService.delete_forwarding_rule(zone_id=zone_id, rule_id=rule_id)
            except Exception as cleanup_err:
                logger.critical(f"Orphaned Cloudflare routing rule leak! Zone: {zone_id}, Rule: {rule_id}. Error: {str(cleanup_err)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal configuration failure.")

    return {
        "status": "active",
        "rule_id": rule_id,
        "alias": f"{clean_prefix}@{domain.name}",
        "destination": target_email
    }

@app.get("/api/v1/get-user", response_model=UserDashboardResponse)
async def get_user_dashboard_records(
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Fetches the user profile along with all associated custom domains, 
    configured routing aliases, and account-wide verified destination inboxes.
    """
    # Eagerly load both parallel 1-to-many relationship vectors in exactly one database roundtrip
    stmt = (
        select(User)
        .where(User.id == user.user_id)
        .options(
            selectinload(User.custom_domains).selectinload(Domain.aliases).selectinload(Alias.destination),
            selectinload(User.destinations)  # 👈 Eager load the destinations array safely here
        )
    )

    try:
        result = await db.execute(stmt)
        db_user = result.scalar_one_or_none()
    except SQLAlchemyError as db_err:
        logger.critical(f"Database driver crash on comprehensive dashboard fetch for {user.user_id}: {str(db_err)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The data tier is temporarily unavailable. Please try again shortly."
        )

    if not db_user:
        # First time this authenticated user has hit the backend (e.g. straight after
        # sign-up, before onboarding a domain). Bootstrap their row instead of 404ing,
        # so the frontend gets a valid empty dashboard rather than an error state.
        db_user = User(id=user.user_id)
        db.add(db_user)
        try:
            await db.commit()
        except IntegrityError:
            # Lost a race against a concurrent request that created the same user first.
            await db.rollback()
            db_user = await db.get(User, user.user_id)
        except Exception as db_err:
            await db.rollback()
            logger.critical(f"Failed to bootstrap new user record for {user.user_id}: {str(db_err)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not initialize your account records."
            )

        # Freshly bootstrapped user has no domains/destinations yet — return the empty shape directly.
        return UserDashboardResponse(
            user_id=db_user.id,
            is_subscribed=db_user.is_subscribed,
            created_at=db_user.created_at,
            domains=[],
            destinations=[]
        )

    return UserDashboardResponse(
        user_id=db_user.id,
        is_subscribed=db_user.is_subscribed,
        created_at=db_user.created_at,
        
        # 1. Map Outbound Custom Infrastructure Domain Trees
        domains=[
            DomainDetailResponse(
                id=domain.id,
                name=domain.name,
                status=domain.status,
                created_at=domain.created_at,
                aliases=[
                    AliasResponse(
                        id=alias.id,
                        alias_name=alias.alias_name,
                        full_email_address=f"{alias.alias_name}@{domain.name}",
                        destination_email=alias.destination.email,  
                        is_active=alias.is_active,
                        forward_count=alias.forward_count,
                        created_at=alias.created_at
                    ) for alias in domain.aliases
                ]
            ) for domain in db_user.custom_domains
        ],
        
        # 2. Map Account Target Receiving Inboxes
        destinations=[
            DestinationResponse(
                id=dest.id,
                email=dest.email,
                status=dest.status,
                created_at=dest.created_at
            ) for dest in db_user.destinations  # Parsed safely from optimized selectinload join
        ]
    )



@app.delete("/api/v1/rules/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_routing_rule(
    alias_id: str,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Permanently deletes an email alias from both Cloudflare and the local database.
    Ensures the user owns the underlying custom domain before deleting.
    """
    # 1. Fetch the alias and its associated domain to verify tenancy ownership
    stmt = (
        select(Alias)
        .where(Alias.id == alias_id)
        .options(selectinload(Alias.domain))
    )
    result = await db.execute(stmt)
    alias = result.scalar_one_or_none()

    # Security Guard: If the alias doesn't exist, or the caller isn't the domain owner, block it.
    if not alias or alias.domain.owner_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested routing alias could not be found under your profile."
        )

    # 2. Delete the resource from Cloudflare Infrastructure
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            logger.info(f"[SANDBOX MODE] Mocking deletion of rule {alias_id} on zone {alias.domain_id}")
        else:
            await CloudflareService.delete_forwarding_rule(
                zone_id=alias.domain_id, 
                rule_id=alias.id
            )
    except HTTPException as cf_err:
        raise cf_err
    except Exception as general_err:
        logger.error(f"Failed to communicate rule deletion to Cloudflare for {alias_id}: {str(general_err)}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream service refused to clear the routing rule infrastructure."
        )

    # 3. Synchronize local database state
    try:
        await db.delete(alias)
        await db.commit()
        logger.info(f"User {user.user_id} successfully deleted alias rule {alias_id}.")
    except Exception as db_err:
        await db.rollback()
        logger.critical(f"Database sync failure deleting alias {alias_id}: {str(db_err)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Infrastructure cleared, but database failed to reflect the change."
        )
    

@app.delete("/api/v1/domains/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_domain(
    zone_id: str,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Deletes a custom domain entirely from Cloudflare infrastructure and wipes out
    the localized record schema alongside all cascading alias sub-rules.
    """
    # 1. Fetch domain and assert tenant ownership
    domain = await db.get(Domain, zone_id)
    if not domain or domain.owner_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested domain resource was not found under your account profile."
        )

    # 2. Wipe the external Cloudflare infrastructure zone
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            logger.info(f"[SANDBOX MODE] Mocking permanent deletion of zone {zone_id} ({domain.name})")
        else:
            await CloudflareService.delete_zone(zone_id)
            
    except HTTPException as cf_err:
        raise cf_err
    except Exception as general_err:
        logger.error(f"Failed to communicate zone decommissioning to Cloudflare for {zone_id}: {str(general_err)}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream cloud provider refused to decommission domain resources."
        )

    # 3. Commit Database Purge (Cascades to child aliases automatically)
    try:
        await db.delete(domain)
        await db.commit()
        logger.info(f"User {user.user_id} completely offboarded domain {domain.name} (Zone: {zone_id}).")
        
    except Exception as db_err:
        await db.rollback()
        logger.critical(f"CRITICAL DB DESYNC: Deleted Cloudflare zone {zone_id}, but local DB commit failed: {str(db_err)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Infrastructure was destroyed, but internal data records failed to sync."
        )
    

@app.delete("/api/v1/destinations/{destination_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_destination_inbox(
    destination_id: str,
    user: UserSession = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session)
):
    """
    Deletes a destination inbox from Cloudflare and wipes it locally.
    Also cascades out any active alias rules relying on this specific target.
    """
    # 1. Fetch destination record and assert tenant multi-ownership security boundaries
    # We load the related aliases to purge them alongside the core record
    stmt = (
        select(Destination)
        .where(Destination.id == destination_id)
        .options(selectinload(Destination.aliases))
    )
    result = await db.execute(stmt)
    destination = result.scalar_one_or_none()
    
    if not destination or destination.user_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested destination record was not found under your profile."
        )

    # 2. Decommission from Cloudflare core account tier
    try:
        if settings.CLOUDFLARE_SANDBOX_MODE:
            logger.info(f"[SANDBOX MODE] Mocking deletion of account destination: {destination.email}")
        else:
            await CloudflareService.delete_destination_address(destination_id)
    except HTTPException as cf_err:
        raise cf_err
    except Exception as general_err:
        logger.error(f"Downstream deletion failure for destination token {destination_id}: {str(general_err)}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream delivery provider rejected the resource drop instruction."
        )

    # 3. Synchronized State Database Clear
    try:
        # Loop through and explicitly delete any forward alias rule records linked to this target
        # to ensure no orphaned foreign keys linger in your SQL storage tier.
        for alias in destination.aliases:
            await db.delete(alias)
            
        # Delete the destination entity itself
        await db.delete(destination)
        await db.commit()
        
        logger.info(f"User {user.user_id} removed destination inbox {destination.email} and cleaned dependent aliases.")
        
    except Exception as db_err:
        await db.rollback()
        logger.critical(f"CRITICAL DESYNC: Dropped CF destination {destination_id}, but database clean failed: {str(db_err)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Infrastructure was destroyed, but database state sync operation failed."
        )
    
