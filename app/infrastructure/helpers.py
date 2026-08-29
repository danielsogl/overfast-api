"""Infrastructure helpers — error reporting and ETag computation."""

import hashlib
import traceback

from fastapi import HTTPException, status

from app.config import settings
from app.infrastructure.logger import logger


def compute_etag(payload: bytes) -> str:
    """Return a weak ETag for the exact response body *payload*.

    Weak, not strong: the tag covers the JSON payload we generate, not the
    octets a downstream proxy finally transfers (content coding may differ),
    and nothing here needs the byte-range/``If-Range`` semantics that a strong
    validator buys. Weak comparison is all ``If-None-Match`` on a GET requires.

    ``blake2b`` at 16 bytes is stdlib, faster than sha256 on the payload sizes
    that matter here (a few hundred KB for a player career), and 128 bits makes
    an accidental collision irrelevant. This is not a security boundary.

    Lives in ``infrastructure`` because both sides need it: the API middleware
    hashes the body FastAPI rendered, and the Valkey adapter hashes the body
    nginx will later print straight out of the cache envelope.
    """
    return f'W/"{hashlib.blake2b(payload, digest_size=16).hexdigest()}"'


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
