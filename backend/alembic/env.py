import os
import sys
from logging.config import fileConfig
from alembic import context

# Ensure backend directory is on sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from config import get_settings, mask_database_url
from database import Base, create_app_engine
import models  # Pure model declarations for all 13 tables; no router imports, no DDL

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model metadata target (contains all 13 tables)
target_metadata = Base.metadata

# Dynamic database URL resolution from centralized settings
settings = get_settings()
db_url = config.get_main_option("sqlalchemy.url")
if not db_url:
    db_url = settings.DATABASE_URL
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Configures the context with just a URL and not an Engine.
    Emits SQL statements directly to the script output without establishing
    a live database connection.
    """
    url = config.get_main_option("sqlalchemy.url") or db_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.
    Creates an Engine and associates a connection with the context.
    Only connects when a migration command is explicitly executed.
    """
    target_url = config.get_main_option("sqlalchemy.url") or db_url
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = create_app_engine(target_url)
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
