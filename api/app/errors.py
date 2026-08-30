"""The error envelope.

Every non-200 response is `{"error": "<machine_code>", "detail": "<human>"}`
with `Cache-Control: no-store` — one shape, decided here, so no route or
framework default can leak a different one. Raise ApiError with an explicit
code; the handlers in main.py render the envelope and also normalize
framework-raised errors (unknown paths, path-type validation) into it.
"""
from fastapi import HTTPException

# Fallback codes for errors raised without one (framework 404s and the like).
DEFAULT_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


class ApiError(HTTPException):
    """HTTPException with a machine-readable code riding along."""

    def __init__(self, status_code: int, code: str, detail: str,
                 headers: dict | None = None):
        super().__init__(status_code=status_code, detail=detail,
                         headers=headers)
        self.code = code


def code_for(exc) -> str:
    return getattr(exc, "code", None) or \
        DEFAULT_CODES.get(exc.status_code, "error")
