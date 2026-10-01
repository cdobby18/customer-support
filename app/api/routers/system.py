"""Liveness, readiness, and knowledge base search."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.knowledge import KnowledgeMatch, search_knowledge
from app.api.dependencies import get_current_user
from app.core.database import get_db, schema_is_current
from app.core.models import UserRecord
from app.core.workers import broker_check_required, broker_is_reachable

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: is this process still running?

    Deliberately touches nothing external. A liveness probe that fails on a
    transient database blip restarts a process that was never broken, so this
    stays a static answer and `/ready` carries the dependency checks.
    """
    return {"status": "ok"}


def _database_is_reachable(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _schema_is_current(db: Session) -> bool:
    return schema_is_current(db.get_bind())


def _broker_state() -> bool | None:
    """True/False when the broker matters, None when it is skipped (eager mode)."""
    if not broker_check_required():
        return None
    return broker_is_reachable()


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness: can this replica serve traffic right now?

    Checks the database, the schema revision, and (outside eager mode) the
    broker. A replica with dead Postgres or an unapplied migration answers 503
    here even though `/health` still says ok, so a load balancer can drain it
    without the container being restarted. That split is the whole point: the
    Docker HEALTHCHECK stays on `/health`, so a database outage never triggers
    a restart loop, while orchestration readiness polls `/ready`.
    """
    checks: dict[str, bool | None] = {
        "database": _database_is_reachable(db),
        "schema": _schema_is_current(db),
        "broker": _broker_state(),
    }
    is_ready = checks["database"] and checks["schema"] and checks["broker"] is not False
    return JSONResponse(
        {
            "status": "ok" if is_ready else "unavailable",
            "checks": {
                name: "skipped" if value is None else ("ok" if value else "failed")
                for name, value in checks.items()
            },
        },
        status_code=200 if is_ready else 503,
    )


@router.get("/knowledge/search", response_model=list[KnowledgeMatch])
def knowledge_search(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=10),
    _: UserRecord = Depends(get_current_user),
) -> list[KnowledgeMatch]:
    return search_knowledge(q, limit=limit)
