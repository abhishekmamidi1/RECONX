from typing import Annotated

from fastapi import APIRouter, Body, Query

from app.api.deps import ActorDep, SessionDep
from app.schemas import CloseRunRequest, CloseRunResponse, RunRequest, RunResponse
from app.services.controller import close_reconciliation
from app.services.reconciliation import run_reconciliation

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.post("/run", response_model=RunResponse)
async def run_reconciliation_endpoint(
    db: SessionDep,
    actor: ActorDep,
    payload: Annotated[RunRequest | None, Body()] = None,
) -> RunResponse:
    summary = await run_reconciliation(
        db,
        actor=actor,
        transaction_ids=payload.transaction_ids if payload else None,
    )
    await db.commit()
    return RunResponse(**summary)


@router.post("/close", response_model=CloseRunResponse)
async def close_reconciliation_endpoint(
    db: SessionDep,
    actor: ActorDep,
    payload: Annotated[CloseRunRequest | None, Body()] = None,
) -> CloseRunResponse:
    result = await close_reconciliation(
        db,
        actor=actor,
        transaction_ids=payload.transaction_ids if payload else None,
    )
    await db.commit()
    return CloseRunResponse(**result)
