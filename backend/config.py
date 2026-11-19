import os
import sys
from urllib.parse import urlsplit
from typing import List, Optional, Union
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, model_validator

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ENV_FILE = os.path.join(_BACKEND_DIR, ".env")


def mask_database_url(url: str) -> str:
    """Mask credentials in a database URL for safe logging and error reporting."""
    if not url:
        return "***"
    try:
        parts = urlsplit(url)
        masked_netloc = parts.netloc
        if "@" in masked_netloc:
            _, host = masked_netloc.split("@", 1)
            masked_netloc = f"***:***@{host}"
        return f"{parts.scheme}://{masked_netloc}{parts.path}"
    except Exception:
        return "***"


class Settings(BaseSettings):
    """
    Centralized, validated application configuration for Carbon AQI System.
    Loads settings from deterministic .env path or environment variables.
    OS environment variables always override file-based .env definitions.
    """

    APP_ENV: str = "development"
    DATABASE_URL: str
    ALLOWED_ORIGINS: Union[List[str], str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    FIREBASE_CREDENTIALS_PATH: Optional[str] = None
    OPENWEATHER_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None

    # Database connection pool hardening (conservative defaults for multi-worker deployments)
    # Estimated max connections = application_workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    # e.g., 4 workers * (5 + 10) = 60 connections (PostgreSQL default max_connections is 100)
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # CORS policy configuration (restricted methods & headers, no wildcards, no cookies needed)
    CORS_ALLOW_CREDENTIALS: bool = False
    CORS_ALLOW_METHODS: List[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    CORS_ALLOW_HEADERS: List[str] = ["Authorization", "Content-Type", "Accept"]

    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", _DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def validate_app_env(cls, v: Optional[str]) -> str:
        if not v or not isinstance(v, str):
            return "development"
        val = v.strip().lower()
        if val not in ("development", "production", "test"):
            raise ValueError(f"Invalid APP_ENV: '{v}'. Must be 'development', 'production', or 'test'.")
        return val

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def validate_database_url(cls, v: Optional[str]) -> str:
        if not v or not isinstance(v, str) or not v.strip():
            raise ValueError("DATABASE_URL is required and cannot be empty.")
        val = v.strip()
        parts = urlsplit(val)
        if not parts.scheme:
            raise ValueError("DATABASE_URL must include a valid database scheme (e.g., postgresql:// or sqlite://).")
        return val

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: Union[str, List[str], None]) -> List[str]:
        if v is None:
            return ["http://localhost:3000", "http://127.0.0.1:3000"]
        if isinstance(v, str):
            raw_list = [item.strip() for item in v.split(",") if item.strip()]
        elif isinstance(v, (list, tuple)):
            raw_list = [str(item).strip() for item in v if str(item).strip()]
        else:
            raw_list = []

        # Deduplicate preserving order
        seen = set()
        deduped = []
        for orig in raw_list:
            if orig not in seen:
                seen.add(orig)
                deduped.append(orig)
        return deduped

    @field_validator("OPENWEATHER_API_KEY", "GROQ_API_KEY", "FIREBASE_CREDENTIALS_PATH", mode="before")
    @classmethod
    def normalize_optional_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            trimmed = v.strip()
            return trimmed if trimmed else None
        return str(v)

    @field_validator("DB_POOL_SIZE", mode="before")
    @classmethod
    def validate_pool_size(cls, v: Union[int, str, None]) -> int:
        if v is None:
            return 5
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"DB_POOL_SIZE must be an integer (got '{v}').")
        if val < 1 or val > 100:
            raise ValueError(f"DB_POOL_SIZE must be between 1 and 100 (got {val}).")
        return val

    @field_validator("DB_MAX_OVERFLOW", mode="before")
    @classmethod
    def validate_max_overflow(cls, v: Union[int, str, None]) -> int:
        if v is None:
            return 10
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"DB_MAX_OVERFLOW must be an integer (got '{v}').")
        if val < 0 or val > 100:
            raise ValueError(f"DB_MAX_OVERFLOW must be between 0 and 100 (got {val}).")
        return val

    @field_validator("DB_POOL_TIMEOUT", mode="before")
    @classmethod
    def validate_pool_timeout(cls, v: Union[int, str, None]) -> int:
        if v is None:
            return 30
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"DB_POOL_TIMEOUT must be an integer (got '{v}').")
        if val < 1 or val > 300:
            raise ValueError(f"DB_POOL_TIMEOUT must be between 1 and 300 seconds (got {val}).")
        return val

    @field_validator("DB_POOL_RECYCLE", mode="before")
    @classmethod
    def validate_pool_recycle(cls, v: Union[int, str, None]) -> int:
        if v is None:
            return 1800
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"DB_POOL_RECYCLE must be an integer (got '{v}').")
        if val < 60 or val > 86400:
            raise ValueError(f"DB_POOL_RECYCLE must be between 60 and 86400 seconds (got {val}).")
        return val

    @model_validator(mode="after")
    def validate_origins_and_environment(self) -> "Settings":
        is_prod = self.APP_ENV == "production"
        origins = self.ALLOWED_ORIGINS if isinstance(self.ALLOWED_ORIGINS, list) else [self.ALLOWED_ORIGINS]

        if is_prod:
            default_dev_origins = {"http://localhost:3000", "http://127.0.0.1:3000"}
            if not origins or set(origins) == default_dev_origins:
                raise ValueError("ALLOWED_ORIGINS is required in production and must contain explicit HTTPS origins (localhost not permitted).")

        normalized = []
        for orig in origins:
            if orig == "*":
                raise ValueError("Wildcard origin '*' is not permitted. Explicit origins must be declared.")

            parts = urlsplit(orig)
            if not parts.scheme or not parts.netloc:
                raise ValueError(f"Malformed origin: '{orig}'. Expected format 'scheme://host[:port]'.")

            # Origins must not contain path components (e.g. /api), query strings, or fragments
            if parts.path not in ("", "/"):
                raise ValueError(f"Origin must not contain path components: '{orig}'.")
            if parts.query or parts.fragment:
                raise ValueError(f"Origin must not contain query or fragment: '{orig}'.")

            scheme = parts.scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(f"Invalid scheme in origin '{orig}'. Only http and https are allowed.")

            norm_orig = f"{scheme}://{parts.netloc}"

            if is_prod:
                if scheme != "https":
                    raise ValueError(f"Production origin must use HTTPS: '{orig}'.")
                host = (parts.hostname or "").lower()
                if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
                    raise ValueError(f"Production origin cannot be localhost or loopback: '{orig}'.")

            if norm_orig not in normalized:
                normalized.append(norm_orig)

        self.ALLOWED_ORIGINS = normalized
        return self

    def __repr__(self) -> str:
        # Strictly mask secrets to prevent accidental credential leakage in logs or exceptions
        masked_db = mask_database_url(self.DATABASE_URL)

        return (
            f"Settings(APP_ENV='{self.APP_ENV}', "
            f"DATABASE_URL='{masked_db}', "
            f"DB_POOL_SIZE={self.DB_POOL_SIZE}, "
            f"DB_MAX_OVERFLOW={self.DB_MAX_OVERFLOW}, "
            f"DB_POOL_TIMEOUT={self.DB_POOL_TIMEOUT}, "
            f"DB_POOL_RECYCLE={self.DB_POOL_RECYCLE}, "
            f"ALLOWED_ORIGINS={self.ALLOWED_ORIGINS}, "
            f"FIREBASE_CREDENTIALS_PATH='{self.FIREBASE_CREDENTIALS_PATH}', "
            f"OPENWEATHER_API_KEY={'***' if self.OPENWEATHER_API_KEY else None}, "
            f"GROQ_API_KEY={'***' if self.GROQ_API_KEY else None}, "
            f"CORS_ALLOW_CREDENTIALS={self.CORS_ALLOW_CREDENTIALS})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@lru_cache()
def get_settings() -> Settings:
    """
    Returns the cached singleton Settings instance.
    Call get_settings.cache_clear() if test environment variables are updated.
    """
    return Settings()
