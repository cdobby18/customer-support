"""Staff-only AI assistance for a single ticket."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_assist import AgentAssistResult, assist_ticket
from app.database import get_db
from app.dependencies import get_current_user
from app.models import TicketCommentRecord, TicketRecord, UserRecord, UserRole

router = APIRouter()


@router.get("/tickets/{ticket_id}/agent-assist", response_model=AgentAssistResult)
def get_agent_assist(
    ticket_id: UUID,
    similar_limit: int = Query(default=3, ge=0, le=10),
    kb_limit: int = Query(default=5, ge=0, le=20),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentAssistResult:
    if current_user.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=403, detail="Support staff access required")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    comments = db.scalars(
        select(TicketCommentRecord)
        .where(TicketCommentRecord.ticket_id == str(ticket_id))
        .order_by(TicketCommentRecord.created_at)
    ).all()
    return assist_ticket(
        db,
        record,
        comments,
        similar_limit=similar_limit,
        kb_limit=kb_limit,
    )
