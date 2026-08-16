# app/database/config.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fastapi import HTTPException
import logging
from app.config import settings


engine_args = {
    "echo": False,
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

if not settings.DATABASE_URL.startswith("sqlite"):
    engine_args["pool_size"] = 10
    engine_args["max_overflow"] = 20

# 1. Establish the lightweight connection pipeline to your local file
engine = create_async_engine( settings.DATABASE_URL, **engine_args )

# 2. Configure our factory class to generate isolated sessions
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

logger = logging.getLogger(__name__)

# 3. The FastAPI dependency provider engine
async def get_db_session() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
        except HTTPException:
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            logger.exception("Unhandled DB session error")
            raise HTTPException(status_code=500, detail="Internal database error.")