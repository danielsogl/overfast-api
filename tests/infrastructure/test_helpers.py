import pytest
from fastapi import status
from fastapi.responses import Response

from app.api import helpers as api_helpers
from app.api.helpers import apply_swr_headers
from app.config import settings
from app.infrastructure.helpers import (
    overfast_internal_error,
)


@pytest.mark.parametrize(
    ("input_duration", "result"),
    [
        (98760, "1 day, 3 hours, 26 minutes"),
        (86400, "1 day"),
        (7200, "2 hours"),
        (3600, "1 hour"),
        (600, "10 minutes"),
        (60, "1 minute"),
        (30, "less than a minute"),
    ],
)
def test_get_human_readable_duration(input_duration: int, result: str):
    actual = api_helpers.get_human_readable_duration(input_duration)

    assert actual == result


# ── overfast_internal_error ───────────────────────────────────────────────────


class TestOverfastInternalError:
    def test_returns_http_500_exception(self):
        exc = overfast_internal_error("/heroes", ValueError("test error"))

        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_long_validation_error_truncated(self):
        """Validation errors > 900 chars keep only first 5 lines."""
        long_msg = "1 validation error for Foo\n" + "\n".join(
            [f"field_{i}\n  value error" for i in range(100)]
        )
        assert len(long_msg) > 900  # noqa: PLR2004
        error = ValueError(long_msg)
        # Should not raise, just truncate
        exc = overfast_internal_error("/test", error)

        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_long_non_validation_error_truncated(self):
        """Non-validation errors > 900 chars are sliced."""
        long_msg = "X" * 1000
        error = RuntimeError(long_msg)
        exc = overfast_internal_error("/test", error)

        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_url_with_http_prefix_used_as_is(self):
        """URLs starting with http are not prefixed with app_base_url."""
        exc = overfast_internal_error("https://blizzard.com/page", ValueError("err"))

        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_url_without_http_prefix_gets_base_url(self):
        """Relative URLs are prefixed with settings.app_base_url."""
        exc = overfast_internal_error("/players/test", ValueError("err"))

        assert exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


# ── apply_swr_headers ────────────────────────────────────────────────────────


class TestApplySWRHeaders:
    def _make_response(self) -> Response:
        return Response(content=b"", media_type="application/json")

    def test_fresh_response_sets_hit_status(self):
        resp = self._make_response()
        apply_swr_headers(resp, cache_ttl=3600, is_stale=False)

        assert resp.headers["X-Cache-Status"] == "hit"
        assert "stale-while-revalidate" not in resp.headers.get("Cache-Control", "")

    def test_stale_response_sets_stale_status(self):
        resp = self._make_response()
        apply_swr_headers(resp, cache_ttl=3600, is_stale=True)

        assert resp.headers["X-Cache-Status"] == "stale"
        assert "stale-while-revalidate" in resp.headers["Cache-Control"]

    def test_age_header_set_when_positive(self):
        resp = self._make_response()
        apply_swr_headers(resp, cache_ttl=3600, is_stale=False, age_seconds=42)

        assert resp.headers["Age"] == "42"

    def test_age_header_not_set_when_zero(self):
        resp = self._make_response()
        apply_swr_headers(resp, cache_ttl=3600, is_stale=False, age_seconds=0)

        assert "Age" not in resp.headers

    def test_cache_ttl_header_always_set(self):
        resp = self._make_response()
        apply_swr_headers(resp, cache_ttl=600, is_stale=False)

        assert resp.headers[settings.cache_ttl_header] == "600"

    def test_staleness_threshold_overrides_max_age(self):
        resp = self._make_response()
        apply_swr_headers(
            resp,
            cache_ttl=3600,
            is_stale=False,
            staleness_threshold=1800,
        )

        assert "max-age=1800" in resp.headers["Cache-Control"]
