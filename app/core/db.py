# app/core/db.py
from supabase import AsyncClient

# Just a simple global variable
supabase_client: AsyncClient | None = None