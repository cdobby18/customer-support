"""FastAPI application wiring.

This module deliberately contains only application construction and
lifespan/middleware setup. Request handling lives in ``app.api.routers.*``,
request/response models in ``app.api.schemas``, and shared business helpers in
``app.agents.support`` / ``app.agents.auto_response``.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routers import admin, agent_assist, auth, system, tickets, webhooks
from app.core.config_validation import validate_security_configuration
from app.core.database import (
    alembic_head_revision,
    current_schema_revision,
    init_db,
)
from app.core.observability import (
    RequestContextMiddleware,
    get_app_logger,
    setup_logging,
    setup_tracing,
)

setup_logging()


def warn_if_schema_not_migrated(logger: logging.Logger) -> None:
    """Log when the database is not at the migration chain's head.

    Deliberately a warning and not an exception. Refusing to start would be
    stricter, but a replica that cannot read `alembic_version` at all (the
    database still starting, a read-only role) would then take a healthy
    deployment down. Refusing to start belongs in the readiness probe, which
    can fail without killing the process.

    This is the failure `create_all()` used to hide: it creates tables that
    Alembic never declared, so a missing revision could pass unnoticed until an
    endpoint queried the absent table. Production skips `create_all()` now, so
    the only thing standing between a deploy and that class of 500 is a
    migration having been run.
    """
    head = alembic_head_revision()
    if head is None:
        logger.warning(
            "could not read the migration chain head; cannot verify the schema"
        )
        return
    current = current_schema_revision()
    if current == head:
        return
    if current is None:
        logger.warning(
            "database has no alembic_version row; run `alembic upgrade head` "
            "(expected %s). The API will serve requests against an unmanaged "
            "schema.",
            head,
        )
        return
    logger.warning(
        "database schema is at revision %s but the migration chain head is %s; "
        "run `alembic upgrade head` before serving traffic.",
        current,
        head,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_security_configuration()
    init_db()
    warn_if_schema_not_migrated(get_app_logger())
    setup_tracing(app)
    yield


app = FastAPI(title="AI Customer Support Automation", lifespan=lifespan)

# `testserver` is the Host header Starlette's TestClient sends, and it is in the
# default on purpose: the whole test suite reaches the app through that client.
# Setting TRUSTED_HOSTS in a CI job therefore does not merely add a host, it
# *replaces* the default and silently drops `testserver`, which turns every API
# test into a 400 "Invalid host header". Any environment that overrides this
# must keep `testserver` or the tests cannot run at all.
DEFAULT_TRUSTED_HOSTS = "localhost,127.0.0.1,testserver"


def parse_trusted_hosts(raw: str | None) -> list[str]:
    if raw is None:
        raw = DEFAULT_TRUSTED_HOSTS
    return [host.strip() for host in raw.split(",") if host.strip()]


trusted_hosts = parse_trusted_hosts(os.getenv("TRUSTED_HOSTS"))
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        # 5174 is the Vite fallback port when 5173 is already taken; both are
        # dev-only and both must be able to call the API from a browser.
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware, logger=get_app_logger())

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(tickets.router)
app.include_router(webhooks.router)
app.include_router(agent_assist.router)
