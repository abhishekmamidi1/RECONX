from app.services.parsers.base import ParseResult, ParserError, SourceParser
from app.services.parsers.bank import BankStatementParser
from app.services.parsers.erp import ErpTransactionParser
from app.services.parsers.razorpay import RazorpaySettlementParser

PARSER_CLASSES: tuple[type[SourceParser], ...] = (
    RazorpaySettlementParser,
    BankStatementParser,
    ErpTransactionParser,
)

PARSERS: dict[str, SourceParser] = {cls.key: cls() for cls in PARSER_CLASSES}


def get_parser(key: str) -> SourceParser:
    parser = PARSERS.get(key)
    if parser is None:
        raise ParserError(f"unknown source type: {key!r}")
    return parser


__all__ = [
    "PARSER_CLASSES",
    "PARSERS",
    "ParseResult",
    "ParserError",
    "SourceParser",
    "get_parser",
]
