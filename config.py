# ceo_remote_backend/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL:                str = ""   # Points to Supabase PostgreSQL
    SECRET_KEY:                  str = ""   # Must match store backend
    ALGORITHM:                   str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    ALLOWED_ORIGINS:             str = "*"

    class Config:
        env_file = ".env"

settings = Settings()
