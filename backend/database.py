from urllib.parse import urlsplit
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import get_settings, mask_database_url

settings = get_settings()
DATABASE_URL = settings.DATABASE_URL


def create_app_engine(database_url: str | None = None, app_settings=None):
    """
    Construct a hardened SQLAlchemy engine with connection health checks,
    pool boundaries, connection recycling, and safe SQLite handling.
    """
    if app_settings is None:
        app_settings = get_settings()
    url = database_url or app_settings.DATABASE_URL

    parts = urlsplit(url)
    scheme = parts.scheme.lower() if parts.scheme else ""

    if scheme.startswith("sqlite"):
        engine_kwargs = {
            "connect_args": {"check_same_thread": False},
            "echo": False,
        }
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            from sqlalchemy.pool import StaticPool
            engine_kwargs["poolclass"] = StaticPool
        return create_engine(url, **engine_kwargs)

    # PostgreSQL / network database configuration
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=app_settings.DB_POOL_SIZE,
        max_overflow=app_settings.DB_MAX_OVERFLOW,
        pool_timeout=app_settings.DB_POOL_TIMEOUT,
        pool_recycle=app_settings.DB_POOL_RECYCLE,
        echo=False,
    )


# Primary application engine
engine = create_app_engine()

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Declarative base
Base = declarative_base()


def init_db(target_engine=None):
    """
    Explicitly initialize database schema for all registered models.
    Called explicitly during application startup or in tests;
    never executed during module import.
    """
    import models  # noqa: F401
    Base.metadata.create_all(bind=target_engine or engine)


def get_db():
    """
    FastAPI dependency that yields an independent SQLAlchemy session per request.
    Guarantees rollback on unhandled exceptions and reliable session closure.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database_health(db_engine=None) -> dict:
    """
    Verify database connectivity using a lightweight query.
    Never exposes passwords or sensitive credentials.
    """
    target_engine = db_engine or engine
    try:
        with target_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        pool = target_engine.pool
        return {
            "status": "healthy",
            "database": mask_database_url(str(target_engine.url)),
            "pool_size": getattr(pool, "size", lambda: None)(),
            "checked_out": getattr(pool, "checkedout", lambda: None)(),
            "overflow": getattr(pool, "overflow", lambda: None)(),
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "database": mask_database_url(str(target_engine.url)),
            "error": "Database connectivity check failed",
        }
