"""RFC 9457 `application/problem+json` — the only error shape this API emits.

Every failure, including FastAPI's own validation errors, is funnelled through
`install_problem_handlers` so a client never has to branch on two error formats.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

CONTENT_TYPE = "application/problem+json"

# `type` is a stable identifier a client may branch on. Relative URIs are legal
# in RFC 9457 and keep the payload readable in a terminal.
TYPE_ABOUT_BLANK = "about:blank"
TYPE_UNAUTHENTICATED = "/problems/unauthenticated"
TYPE_FORBIDDEN = "/problems/forbidden"
TYPE_CSRF = "/problems/csrf-token-invalid"
TYPE_RATE_LIMITED = "/problems/rate-limited"
TYPE_CONFLICT = "/problems/conflict"
TYPE_VALIDATION = "/problems/validation-failed"
TYPE_INVITE_INVALID = "/problems/invite-invalid"
TYPE_NOT_FOUND = "/problems/not-found"
TYPE_UNPROCESSABLE = "/problems/unprocessable"


class Problem(Exception):
    """An error that already knows how it serialises."""

    def __init__(
        self,
        *,
        status_code: int,
        title: str,
        detail: str,
        type_: str = TYPE_ABOUT_BLANK,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.type = type_
        self.headers = headers or {}
        self.extra = extra

    def to_response(self, instance: str | None = None) -> JSONResponse:
        body: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance:
            body["instance"] = instance
        body.update(self.extra)
        return JSONResponse(
            body,
            status_code=self.status_code,
            media_type=CONTENT_TYPE,
            headers=self.headers,
        )


def unauthenticated(detail: str = "Sign in to continue.") -> Problem:
    return Problem(
        status_code=status.HTTP_401_UNAUTHORIZED,
        title="Not authenticated",
        detail=detail,
        type_=TYPE_UNAUTHENTICATED,
    )


def forbidden(*, missing_permission: str) -> Problem:
    """403 naming the permission the caller lacks (PRD §14)."""
    return Problem(
        status_code=status.HTTP_403_FORBIDDEN,
        title="Not authorized",
        detail=f"This action requires the '{missing_permission}' permission.",
        type_=TYPE_FORBIDDEN,
        missing_permission=missing_permission,
    )


def conflict(detail: str, *, title: str = "Conflict", **extra: Any) -> Problem:
    return Problem(
        status_code=status.HTTP_409_CONFLICT,
        title=title,
        detail=detail,
        type_=TYPE_CONFLICT,
        **extra,
    )


def not_found(detail: str, *, title: str = "Not found") -> Problem:
    return Problem(
        status_code=status.HTTP_404_NOT_FOUND,
        title=title,
        detail=detail,
        type_=TYPE_NOT_FOUND,
    )


def unprocessable(detail: str, *, title: str = "Unprocessable request", **extra: Any) -> Problem:
    """422 for a request that parsed but asks for something impossible."""
    return Problem(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        title=title,
        detail=detail,
        type_=TYPE_UNPROCESSABLE,
        **extra,
    )


def install_problem_handlers(app: FastAPI) -> None:
    """Route every error class through the problem+json shape."""

    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return exc.to_response(instance=request.url.path)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem = Problem(
            status_code=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
            headers=dict(exc.headers or {}),
        )
        return problem.to_response(instance=request.url.path)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Validation failed",
            detail="The request body did not match the expected shape.",
            type_=TYPE_VALIDATION,
            # `errors()` can carry the offending input; drop it so a rejected
            # password never reaches a client or a log.
            errors=[
                {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                for err in exc.errors()
            ],
        )
        return problem.to_response(instance=request.url.path)
