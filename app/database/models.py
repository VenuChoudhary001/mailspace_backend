import enum
from datetime import datetime, timezone
from typing import List
from sqlalchemy import UUID, Column, String, ForeignKey, DateTime, Boolean, Enum, Index, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from datetime import datetime, timezone, timedelta
import uuid
# 1. Base Class for Model Tracking Inheritance
class Base(DeclarativeBase):
    pass

# Structural tracking enums
class DomainStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"

class DestinationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"


# 2. The Users Table Configuration
class User(Base):
    __tablename__ = "users"
    
    # We map Clerk's unique external user_id string directly as the Primary Key
    id: Mapped[str] = mapped_column(String, primary_key=True) 
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    
    # Explicit, non-ambiguous relationship mappings
    custom_domains: Mapped[List["Domain"]] = relationship(
        back_populates="owner", 
        cascade="all, delete-orphan"
    )
    destinations: Mapped[List["Destination"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )
    smtp_credentials: Mapped[List["SmtpCredential"]] = relationship("SmtpCredential", back_populates="user", cascade="all, delete-orphan")


# 3. The Custom Domains Table Configuration
class Domain(Base):
    __tablename__ = "domains"
    
    # We use Cloudflare's unique zone_id token string directly as our Primary Key
    id: Mapped[str] = mapped_column(String, primary_key=True) 
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False) # e.g., "business.com"
    status: Mapped[DomainStatus] = mapped_column(Enum(DomainStatus), default=DomainStatus.PENDING)
    
    # Foreign Key structural link to track domain tenancy
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    
    # Relationship links
    owner: Mapped["User"] = relationship(back_populates="custom_domains")
    aliases: Mapped[List["Alias"]] = relationship(
        back_populates="domain", 
        cascade="all, delete-orphan"
    )

    # Performance Indexing: Speeds up lookup requests validating user-owned domains
    __table_args__ = (Index("idx_domains_owner_name", "owner_id", "name"),)


# 4. The Destination Inboxes Table Configuration
class Destination(Base):
    __tablename__ = "destinations"
    
    # Cloudflare's verified address string/token maps directly as our identifier
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False) # e.g., "personal@gmail.com"
    status: Mapped[DestinationStatus] = mapped_column(Enum(DestinationStatus), default=DestinationStatus.PENDING)
    
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    
    # Relationship links
    user: Mapped["User"] = relationship(back_populates="destinations")
    aliases: Mapped[List["Alias"]] = relationship(back_populates="destination")

    # Guard Index: A user can't register the exact same destination inbox twice
    __table_args__ = (Index("idx_user_dest_lookup", "user_id", "email", unique=True),)


# 5. The Aliases Table Configuration
class Alias(Base):
    __tablename__ = "aliases"
    
    # Matches Cloudflare's unique routing rule tracking ID string
    id: Mapped[str] = mapped_column(String, primary_key=True)
    alias_name: Mapped[str] = mapped_column(String, nullable=False) # e.g., "support" (yields support@business.com)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    forward_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
    
    # Foreign Keys connecting the mapping logic cleanly
    domain_id: Mapped[str] = mapped_column(String, ForeignKey("domains.id"), nullable=False)
    destination_id: Mapped[str] = mapped_column(String, ForeignKey("destinations.id"), nullable=False)
    
    # Relationship links
    domain: Mapped["Domain"] = relationship(back_populates="aliases")
    destination: Mapped["Destination"] = relationship(back_populates="aliases")

    # Guard Index: Enforces absolute prefix uniqueness per custom domain partition
    __table_args__ = (Index("idx_alias_lookup", "domain_id", "alias_name", unique=True),)

class SmtpCredential(Base):
    __tablename__ = "smtp_credentials"

    # Keeps it as UUID for the primary key (excellent for security/unguessable IDs)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # FIXED: Changed from UUID to String to perfectly match User.id (Clerk ID)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Modernized columns using standard Mapped types
    smtp_username: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    smtp_password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        onupdate=lambda: datetime.now(timezone.utc), 
        nullable=False
    )

    # Relationship back to the core User model
    user: Mapped["User"] = relationship(back_populates="smtp_credentials")