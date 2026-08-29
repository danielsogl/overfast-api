"""Infrastructure helpers — error reporting."""

import traceback

from fastapi import HTTPException, status

from app.config import settings
from app.infrastructure.logger import logger


def overfast_internal_error(url: str, error: Exception) -> HTTPException:
    """Return an Internal Server Error HTTPException.

    Also logs the error at CRITICAL level with the full traceback. Called from
    domain services and the API exception handler for unexpected parsing
    failures.
    """
    logger.critical(
        "Internal server error for URL {} : {}\n{}",
        url,
        error,
        traceback.format_exc(),
    )

    # If we're using a profiler, it means we're debugging, raise the error
    # directly in order to have proper backtrace in logs
    if settings.profiler:
        raise error  # pragma: no cover

    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=settings.internal_server_error_message,
    )
