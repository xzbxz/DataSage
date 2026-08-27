"""Single owner for SQL identifier validation and quoting."""

from __future__ import annotations

import re


_COLUMN_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_TABLE_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)


class SqlIdentifierError(ValueError):
    pass


def quote_identifier(value: str) -> str:
    if not isinstance(value, str) or _COLUMN_IDENTIFIER.fullmatch(value) is None:
        raise SqlIdentifierError("invalid column identifier")
    return f"`{value}`"


def quote_table(value: str) -> str:
    if not isinstance(value, str) or _TABLE_IDENTIFIER.fullmatch(value) is None:
        raise SqlIdentifierError("invalid table identifier")
    return ".".join(quote_identifier(part) for part in value.split("."))


def qualified_identifier(alias: str, column: str) -> str:
    return f"{quote_identifier(alias)}.{quote_identifier(column)}"
