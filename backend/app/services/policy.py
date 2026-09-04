from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PolicyConfig


async def load_policy(db: AsyncSession) -> dict:
    rows = (await db.execute(select(PolicyConfig))).scalars().all()
    return {row.key: row.value for row in rows}
