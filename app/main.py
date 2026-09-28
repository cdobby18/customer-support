"""FastAPI application wiring.

This module deliberately contains only application construction and
lifespan/middleware setup. Request handling lives in ``app.routers.*``,
request/response models in ``app.schemas``, and shared business helpers in
``app.support`` / ``app.auto_response``.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.database import init_db
from app.observability import (
    RequestContextMiddleware,
    get_app_logger,
    setup_logging,
    setup_tracing,
)
from app.routers import admin, agent_assist, auth, system, tickets, webhooks
from app.security import validate_security_configuration

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_security_configuration()
    init_db()
    setup_tracing(app)
    yield


app = FastAPI(title="AI Customer Support Automation", lifespan=lifespan)

trusted_hosts = [
    host.strip()
    for host in os.getenv("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if host.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
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
