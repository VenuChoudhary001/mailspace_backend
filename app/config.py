import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    CLOUDFLARE_API_URL: str = "https://api.cloudflare.com/client/v4"
    CLOUDFLARE_API_TOKEN: str = os.getenv("CLOUDFLARE_API_TOKEN", "")
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    CLOUDFLARE_SANDBOX_MODE: bool = os.getenv("CLOUDFLARE_SANDBOX_MODE", "False").lower() in ("true", "1", "t")
    CLERK_JWKS_URL:str = os.getenv("CLERK_JWKS_URL")  # e.g., https://clerk.yourdomain.com/.well-known/jwks.json
    CLERK_AUDIENCE:str = os.getenv("CLERK_AUDIENCE") 
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mailspace.db")
    ALLOWED_ORIGINS: list[str] = [
        origin.strip() for origin in os.getenv(
            "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",") if origin.strip()
    ]
   

    def __init__(self):
        missing = [
            name for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
            if not getattr(self, name)
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    @property
    def cloudflare_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        }

settings = Settings()