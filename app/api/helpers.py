"""API Helpers module"""

from functools import cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import status

from app.api.models.errors import (
    BlizzardErrorMessage,
    BlizzardRateLimitErrorMessage,
    InternalServerErrorMessage,
    RateLimitErrorMessage,
)
from app.config import settings

if TYPE_CHECKING:
    from fastapi import Request, Response

# Typical routes responses to return
success_responses: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "description": "Successful Response",
        "headers": {
            settings.cache_ttl_header: {
                "description": "The TTL value for the cached response, in seconds",
                "schema": {
                    "type": "string",
                    "example": "600",
                },
            },
            "Age": {
                "description": (
                    "Number of seconds since the response payload was generated "
                    "(RFC 7234 §5.1). Present on FastAPI-served responses; "
                    "also set by nginx for Valkey cache hits."
                ),
                "schema": {
                    "type": "string",
                    "example": "42",
                },
            },
            "Cache-Control": {
                "description": (
                    "Standard caching directives (RFC 7234 + RFC 5861). "
                    "``max-age`` reflects the staleness threshold in seconds. "
                    "``stale-while-revalidate`` is present when a background "
                    "refresh is in-flight, indicating how long stale data may "
                    "still be served."
                ),
                "schema": {
                    "type": "string",
                    "example": "public, max-age=86400, stale-while-revalidate=60",
                },
            },
            "X-Cache-Status": {
                "description": (
                    "Indicates whether the response was served from a fresh "
                    "cache entry (``hit``) or a stale one while a background "
                    "refresh is in-flight (``stale``)."
                ),
                "schema": {
                    "type": "string",
                    "enum": ["hit", "stale"],
                    "example": "hit",
                },
            },
        },
    },
}

routes_responses: dict[int | str, dict[str, Any]] = {
    **success_responses,
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": RateLimitErrorMessage,
        "description": "API Rate Limit Error",
        "headers": {
            settings.retry_after_header: {
                "description": "Indicates how long to wait before making a new request",
                "schema": {
                    "type": "string",
                    "example": "1",
                },
            }
        },
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": BlizzardRateLimitErrorMessage,
        "description": "Blizzard Rate Limit Error",
        "headers": {
            settings.retry_after_header: {
                "description": "Indicates how long to wait before making a new request",
                "schema": {
                    "type": "string",
                    "example": "5",
                },
            }
        },
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": InternalServerErrorMessage,
        "description": "Internal Server Error",
    },
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "model": BlizzardErrorMessage,
        "description": "Blizzard Server Error",
    },
}


@cache
def get_human_readable_duration(duration: int) -> str:
    # Define the time units
    days, remainder = divmod(duration, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    # Build the human-readable string
    duration_parts = []
    if days > 0:
        duration_parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours > 0:
        duration_parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0:
        duration_parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")

    if not duration_parts:
        return "less than a minute"

    return ", ".join(duration_parts)


def build_cache_key(request: Request) -> str:
    """Build a canonical cache key from the request path and its *declared*
    query parameters.

    Only the parameters the route actually declares take part. FastAPI ignores
    every other one when producing the response, so including them in the key
    meant ``?zzz=1``, ``?zzz=2``, … each stored a separate copy of the identical
    payload for 24h. Valkey runs ``volatile-lru``, so that evicts legitimate
    entries: cache pollution costing one request per entry, from anyone.

    Sorting keeps ``?a=1&b=2`` and ``?b=2&a=1`` on one entry.

    The path is taken raw (percent-encoded) because nginx reads this same cache
    under ``api-cache:<ngx.var.request_uri>``, and its path is never decoded.
    Using the decoded path made every Blizzard-ID request — they contain ``%7C``
    by design — a permanent nginx miss against a key only the app could read.
    nginx never writes, so a key it cannot reconstruct costs the fast path, not
    correctness.
    """
    raw_path = request.scope.get("raw_path")
    # Per the ASGI spec raw_path excludes the query string, but not every server
    # honours that; splitting is cheap and correct either way.
    path = raw_path.decode("ascii").split("?", 1)[0] if raw_path else request.url.path

    route = request.scope.get("route")
    declared = (
        {field.alias for field in route.dependant.query_params}
        if route is not None and hasattr(route, "dependant")
        else None
    )
    if declared is None:
        # No route in scope (shouldn't happen inside a handler). Fall back to
        # the previous behaviour rather than silently dropping real parameters.
        return f"{path}?{request.url.query}" if request.url.query else path

    params = sorted(
        (key, value)
        for key, value in request.query_params.multi_items()
        if key in declared
    )
    query = urlencode(params)
    return f"{path}?{query}" if query else path


def apply_swr_headers(
    response: Response,
    cache_ttl: int,
    is_stale: bool,
    age_seconds: int = 0,
    *,
    staleness_threshold: int | None = None,
) -> None:
    """Add standard SWR and cache metadata headers to the response.

    Sets ``Cache-Control`` (RFC 5861), ``Age`` (RFC 7234), ``X-Cache-Status``,
    and the non-standard ``X-Cache-TTL`` on every FastAPI-served response.

    ``staleness_threshold`` is used for ``Cache-Control: max-age``; it defaults
    to ``cache_ttl`` for endpoints that have no SWR (e.g. player, search).
    ``stale-while-revalidate`` is included only on stale responses, using the
    configured ``stale_cache_timeout`` as the revalidation window.
    """
    max_age = staleness_threshold if staleness_threshold is not None else cache_ttl
    response.headers[settings.cache_ttl_header] = str(cache_ttl)
    if age_seconds > 0:
        response.headers["Age"] = str(age_seconds)
    if is_stale:
        response.headers["Cache-Control"] = (
            f"public, max-age={max_age},"
            f" stale-while-revalidate={settings.stale_cache_timeout}"
        )
        response.headers["X-Cache-Status"] = "stale"
    else:
        response.headers["Cache-Control"] = f"public, max-age={max_age}"
        response.headers["X-Cache-Status"] = "hit"
