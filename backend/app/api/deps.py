from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_actor(x_actor: Annotated[str | None, Header(alias="X-Actor")] = None) -> str:
    actor = (x_actor or "").strip()
    return actor or get_settings().default_actor


ActorDep = Annotated[str, Depends(get_actor)]
