# app/core/db.py
import asyncio
from supabase import AsyncClient
from app.core.config import settings

supabase_client: AsyncClient | None = None
