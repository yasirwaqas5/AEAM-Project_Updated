"""
migrations/env.py

Alembic environment for AEAM (Phase E5 — Persistence Evolution).

Connection truth: the database URL is resolved from the SAME
``Settings.DATABASE_URL`` the application itself uses — never duplicated
into ``alembic.ini`` — so migrations and the running app can never drift
onto different databases, and no credential lands in a tracked file
(SEC-5, ENG-6: one configuration mechanism).

An explicit ``-x db_url=...`` override is supported for the migration
test-suite (which runs against throwaway SQLite files) and for ops
one-offs against a specific instance:

    alembic -x db_url=sqlite:///./tmp.db upgrade head

Dialect portability: AEAM runs PostgreSQL in production and SQLite in
tests. SQLite cannot ALTER a column in place, so ``render_as_batch`` is
enabled — Alembic then emits the copy-and-move table recipe automatically
under SQLite while emitting plain ALTER statements under PostgreSQL.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the `aeam` package importable when alembic runs from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# AEAM's schema is declared as explicit DDL (aeam/integrations/database.py
# and aeam/integrations/enterprise_schema.py), not as SQLAlchemy ORM
# models. There is therefore no MetaData object to autogenerate against —
# revisions are written explicitly, which is also what COMPAT-5 wants:
# every schema change is a reviewed, intentional artifact.
target_metadata = None


def _resolve_database_url() -> str:
    """
    Resolve the database URL, in precedence order:

    1. ``-x db_url=...`` on the alembic command line (tests / ops one-offs)
    2. ``DATABASE_URL`` in the environment
    3. ``Settings.DATABASE_URL`` (which itself reads env + .env)

    Raises:
        RuntimeError: if no URL can be resolved — failing loudly beats
                      silently migrating the wrong database.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db_url"):
        return x_args["db_url"]

    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    try:
        from aeam.config.settings import Settings

        return str(Settings().DATABASE_URL)  # pyright: ignore[reportCallIssue]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Could not resolve a database URL for Alembic. Provide one via "
            "`-x db_url=...`, the DATABASE_URL environment variable, or a "
            "valid .env consumable by aeam.config.settings.Settings."
        ) from exc


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations transactionally."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite has no in-place ALTER COLUMN; batch mode makes the
            # same revision script work on both dialects unchanged.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
