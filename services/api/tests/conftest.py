import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

import cvops_api.db.models  # noqa: F401 — registers all 21 models with Base.metadata
from cvops_api.db.base import Base
from cvops_api.engine.step import Step, StepContext


@pytest.fixture(scope="session")
def postgres_url() -> str:  # type: ignore[return]
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()
        # testcontainers returns a psycopg2 URL; swap driver for asyncpg
        url = url.replace("+psycopg2", "+asyncpg").replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        # strip any remaining psycopg2 fragment
        if "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield url


@pytest.fixture(scope="session", autouse=True)
def create_test_schema(postgres_url: str) -> None:
    """Create all tables once for the entire test session."""

    async def _setup() -> None:
        engine = create_async_engine(postgres_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


@pytest.fixture
def fake_redis():  # type: ignore[no-untyped-def]
    """Install a fakeredis client as the process-global Redis used by
    `get_redis()` (so `enqueue_step` XADDs land somewhere inspectable), then
    restore the previous client on teardown."""
    import fakeredis.aioredis

    from cvops_api.core import redis_client

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    prev = redis_client._redis
    redis_client._redis = fake
    yield fake
    redis_client._redis = prev


class EchoStep(Step):
    """Trivial step for engine tests — no ffmpeg/S3, just echoes its inputs."""

    type_key = "test.echo"
    config_schema: dict[str, Any] = {"type": "object"}

    async def run(
        self, ctx: StepContext, config: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {"echoed": inputs}


@pytest.fixture
def echo_step():  # type: ignore[no-untyped-def]
    """Register EchoStep in the global registry for the duration of a test."""
    from cvops_api.core.registry import registry

    step = EchoStep()
    registry.register(step)
    yield step
    registry._store.pop(step.type_key, None)


@pytest.fixture
async def session(postgres_url: str) -> AsyncSession:  # type: ignore[return]
    """
    Per-test async DB session.
    Tests use unique UUID-based values so rows from different tests never collide.
    Call await session.flush() to make inserts visible within the test.
    Call await session.commit() when the test needs to verify post-commit state.
    """
    engine = create_async_engine(postgres_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture(autouse=True)
def _auth_mode_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTH_MODE has no default (startup refuses to run without it); the suite
    exercises the JWT login, so every test runs with AUTH_MODE=local unless it
    overrides it."""
    from cvops_api.config import settings

    monkeypatch.setattr(settings, "AUTH_MODE", "local")
