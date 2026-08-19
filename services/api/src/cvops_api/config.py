from pydantic_settings import BaseSettings, SettingsConfigDict

# Known placeholder values that must never be used in production.
_INSECURE_DEFAULTS: frozenset[str] = frozenset({
    "change-me-in-production-min-32-chars",
    "change-me-worker-token",
    "GKchangeme",
    "changeme",
})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://cvops:cvops@localhost:5432/cvops"

    # S3-compatible storage (Garage by default; works against AWS S3, MinIO, etc.)
    S3_ENDPOINT: str = "http://localhost:3900"
    S3_ACCESS_KEY: str = "GKchangeme"
    S3_SECRET_KEY: str = "changeme"
    S3_BUCKET: str = "cvops-blobs"
    S3_REGION: str = "garage"
    S3_BACKEND: str = "garage"  # value recorded in blobs.storage_backend
    # Browser-reachable endpoint for presigned URLs. S3_ENDPOINT (e.g.
    # http://garage:3900) is internal and unresolvable from a browser. When this
    # is empty (the default), the endpoint is derived per-request from the Host
    # header as http://<request-host>:S3_PUBLIC_PORT — so it auto-adapts whether
    # the page is opened at localhost or a dev VM. Set it only to force a fixed
    # host/scheme (e.g. behind HTTPS).
    S3_PUBLIC_ENDPOINT: str = ""
    S3_PUBLIC_PORT: int = 3900

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Auth
    JWT_SECRET: str = "change-me-in-production-min-32-chars"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    WORKER_TOKEN: str = "change-me-worker-token"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # CVAT (Phase 2)
    CVAT_URL: str = "http://cvat:8080"
    CVAT_USERNAME: str = "admin"
    CVAT_PASSWORD: str = "cvops"

    # Model Deployer
    MODEL_DEPLOYER_URL: str = "http://model-deployer:8001"

    # Set to true only for local development when real secrets are not yet configured.
    ALLOW_INSECURE_DEFAULTS: bool = False


def validate_secrets(s: Settings) -> None:
    """Raise at server startup if critical secrets are still set to known placeholders.

    Called from the FastAPI lifespan, not at import time, so the test suite can
    import Settings freely without needing real secrets in the test environment.
    Bypass with ALLOW_INSECURE_DEFAULTS=true for local development only.
    """
    if s.ALLOW_INSECURE_DEFAULTS:
        return
    flagged = [
        name
        for name, value in (
            ("JWT_SECRET", s.JWT_SECRET),
            ("WORKER_TOKEN", s.WORKER_TOKEN),
            ("S3_ACCESS_KEY", s.S3_ACCESS_KEY),
            ("S3_SECRET_KEY", s.S3_SECRET_KEY),
        )
        if value in _INSECURE_DEFAULTS or value.lower().startswith("change")
    ]
    if flagged:
        raise RuntimeError(
            f"Refusing to start with placeholder secrets: {', '.join(flagged)}. "
            "Set real values in your .env file. "
            "For local dev only, set ALLOW_INSECURE_DEFAULTS=true."
        )


settings = Settings()
