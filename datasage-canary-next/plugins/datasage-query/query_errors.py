"""Shared query failures across validation, execution and public evidence."""
from __future__ import annotations

from typing import Any, Mapping


class QueryFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        timeout: bool = False,
        stage: str | None = None,
        retryable: bool | None = None,
        source_evidence_ref: Mapping[str, Any] | None = None,
        path: str | None = None,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.timeout = timeout
        self.stage = stage
        self.retryable = retryable
        self.path = path
        self.hint = hint
        self.source_evidence_ref = (
            dict(source_evidence_ref)
            if isinstance(source_evidence_ref, Mapping)
            else None
        )
