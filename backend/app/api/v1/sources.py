from fastapi import APIRouter

from app.schemas import SourceInfoOut
from app.services.parsers import PARSER_CLASSES

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceInfoOut])
async def list_sources() -> list[SourceInfoOut]:
    return [
        SourceInfoOut(
            key=parser.key,
            label=parser.label,
            required_columns=sorted(parser.required_columns),
            optional_columns=sorted(parser.optional_columns),
        )
        for parser in PARSER_CLASSES
    ]
