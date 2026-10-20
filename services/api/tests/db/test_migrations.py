"""Guardrails for the Alembic migration chain (#194).

The rest of the suite builds its schema with ``Base.metadata.create_all``, so
until these tests nothing ever ran the migrations. That's how 0005 shipped
without the ``model_artifacts`` id/deleted_at indexes, and how a randomly named
``cfb17149867a_tilt_generated.py`` reached the chain unnoticed.

Each test that touches a database gets its own throwaway database in the
session's testcontainers Postgres, so the ``create_all`` schema used by every
other test is never touched.

The tests are sync on purpose: ``alembic/env.py`` calls ``asyncio.run()``,
which can't run inside pytest-asyncio's event loop.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from cvops_api.db.base import Base

API_DIR = Path(__file__).resolve().parents[2]
VERSIONS_DIR = API_DIR / "alembic" / "versions"

# 000N_description.py. A one-letter suffix (0007a_) is allowed only to slot an
# existing revision between two numbers without renumbering what follows.
FILENAME_RE = re.compile(r"^\d{4}[a-z]?_[a-z0-9_]+\.py$")

# Renamed from cfb17149867a_tilt_generated.py; its ID must never change (see
# 0007a_model_artifacts_indexes.py).
LEGACY_REVISION = "cfb17149867a"


def _alembic_config() -> Config:
    # No ini file on purpose: env.py would fileConfig() it and reset the logging
    # configuration of the whole test session.
    cfg = Config()
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


def _head() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None
    return head


async def _admin(postgres_url: str, sql: str) -> None:
    engine = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(sql))
    await engine.dispose()


async def _current_and_drift(url: str) -> tuple[str | None, list[object]]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        drift = await conn.run_sync(
            lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
        )
    await engine.dispose()
    return current, drift


@pytest.fixture
def scratch_db(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, empty database; env.py reads its URL from DATABASE_URL."""
    name = f"migtest_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(postgres_url, f'CREATE DATABASE "{name}"'))
    url = make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    yield url
    asyncio.run(_admin(postgres_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


# ── static checks (no database) ───────────────────────────────────────────────


def test_migration_filenames_follow_the_convention() -> None:
    bad = sorted(
        p.name
        for p in VERSIONS_DIR.glob("*.py")
        if p.name != "__init__.py" and not FILENAME_RE.match(p.name)
    )
    assert not bad, (
        f"Migration files must be named 000N_description.py, got {bad}. "
        "Create migrations with services/api/scripts/new-migration.sh."
    )


def test_chain_is_linear_with_a_single_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert len(script.get_heads()) == 1, f"multiple heads: {script.get_heads()}"
    for rev in script.walk_revisions():
        assert not isinstance(rev.down_revision, tuple | list), (
            f"{rev.revision} is a merge revision; keep the chain linear"
        )


def test_legacy_revision_id_is_preserved() -> None:
    """Databases that applied the stray migration store its ID, and 0008
    depends on it, so renaming the file must never change the ID."""
    script = ScriptDirectory.from_config(_alembic_config())
    rev = script.get_revision(LEGACY_REVISION)
    assert rev is not None
    assert Path(rev.path).name == "0007a_model_artifacts_indexes.py"
    assert rev.down_revision == "0007"


# ── against a real database ───────────────────────────────────────────────────


def test_fresh_database_upgrades_to_a_schema_matching_the_models(scratch_db: str) -> None:
    """A new database migrated to head must match the ORM models exactly. An
    autogenerate diff here means a model change shipped without its migration
    (the way 0005 shipped without two indexes)."""
    command.upgrade(_alembic_config(), "head")

    current, drift = asyncio.run(_current_and_drift(scratch_db))
    assert current == _head()
    assert drift == [], f"models and migrations disagree: {drift}"


def test_database_at_the_legacy_revision_upgrades_cleanly(scratch_db: str) -> None:
    """An existing dev database that stopped at the old tilt-generated head
    (its alembic_version row says cfb17149867a) still upgrades to head after
    the rename, and re-running the upgrade at head is a no-op."""
    cfg = _alembic_config()
    command.upgrade(cfg, LEGACY_REVISION)
    assert asyncio.run(_current_and_drift(scratch_db))[0] == LEGACY_REVISION

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # already at head: nothing to do

    current, drift = asyncio.run(_current_and_drift(scratch_db))
    assert current == _head()
    assert drift == []
