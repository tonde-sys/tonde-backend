"""Staff-only QAPE human review endpoints. No direct ticket creation."""
from typing import Literal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.deps import get_current_supervisor
from app.models.user import User
from app.services.qape_review import QapeReviewService

router = APIRouter()


class HumanReviewInput(BaseModel):
    action: Literal['APPROVE', 'REJECT']
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('A nonblank reason is required')
        return value


@router.get('/organizations/{org_id}/branches/{agency_id}/reviews')
async def pending_reviews(org_id: str, agency_id: str,
                          service_id: str | None = None,
                          page: int = Query(1, ge=1),
                          page_size: int = Query(25, ge=1, le=100),
                          actor: User = Depends(get_current_supervisor),
                          db: AsyncSession = Depends(get_db)):
    return await QapeReviewService(db).pending(actor, org_id, agency_id,
                                               service_id, page, page_size)


@router.post('/organizations/{org_id}/reviews/{operation_id}/decision')
async def decide_review(org_id: str, operation_id: str, body: HumanReviewInput,
                        actor: User = Depends(get_current_supervisor),
                        db: AsyncSession = Depends(get_db)):
    return await QapeReviewService(db).decide(actor, org_id, operation_id,
                                               body.action, body.reason)
