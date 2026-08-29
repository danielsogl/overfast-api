"""Conditional requests (ETag / If-None-Match) on the FastAPI response path.

pytest only ever reaches FastAPI, which is the cache-*miss* path. A cache hit is
answered by nginx straight out of Valkey and never gets here — that half is
asserted by ``scripts/smoke-test.sh``. What is checkable from here is the tag
nginx will hand out: it lives in the cache envelope, and the last test asserts it
covers exactly the bytes nginx prints.
"""

import json
from compression import zstd
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from fastapi import status

from app.config import settings
from app.infrastructure.helpers import compute_etag
from tests.helpers import read_html_file

if TYPE_CHECKING:
    import fakeredis
    from fastapi.testclient import TestClient

CACHE_HEADERS = ("Cache-Control", "X-Cache-Status", settings.cache_ttl_header)


@pytest.fixture(autouse=True)
def _setup_heroes_page():
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_200_OK, text=read_html_file("heroes.html")
        ),
    ):
        yield


def test_cacheable_response_carries_a_weak_etag(client: TestClient):
    response = client.get("/heroes")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["ETag"].startswith('W/"')


def test_etag_covers_the_body_actually_sent(client: TestClient):
    response = client.get("/heroes")

    expected = compute_etag(response.content)

    assert response.headers["ETag"] == expected


def test_identical_requests_share_one_etag(client: TestClient):
    first = client.get("/heroes")

    second = client.get("/heroes")

    assert second.headers["ETag"] == first.headers["ETag"]


def test_different_payloads_get_different_etags(client: TestClient):
    everyone = client.get("/heroes")

    tanks = client.get("/heroes", params={"role": "tank"})

    assert tanks.headers["ETag"] != everyone.headers["ETag"]


def test_matching_if_none_match_returns_304_without_a_body(client: TestClient):
    etag = client.get("/heroes").headers["ETag"]

    response = client.get("/heroes", headers={"If-None-Match": etag})

    assert response.status_code == status.HTTP_304_NOT_MODIFIED
    assert response.content == b""


def test_304_keeps_the_cache_metadata_headers(client: TestClient):
    etag = client.get("/heroes").headers["ETag"]

    response = client.get("/heroes", headers={"If-None-Match": etag})

    assert all(header in response.headers for header in CACHE_HEADERS)
    assert response.headers["ETag"] == etag


def test_304_does_not_announce_a_content_length(client: TestClient):
    etag = client.get("/heroes").headers["ETag"]

    response = client.get("/heroes", headers={"If-None-Match": etag})

    assert "content-length" not in response.headers


def test_strong_form_of_the_tag_still_matches(client: TestClient):
    etag = client.get("/heroes").headers["ETag"]

    response = client.get("/heroes", headers={"If-None-Match": etag.removeprefix("W/")})

    assert response.status_code == status.HTTP_304_NOT_MODIFIED


def test_outdated_if_none_match_returns_the_full_payload(client: TestClient):
    reference = client.get("/heroes")

    response = client.get("/heroes", headers={"If-None-Match": 'W/"outdated"'})

    assert response.status_code == status.HTTP_200_OK
    assert response.content == reference.content


def test_error_responses_are_not_tagged(client: TestClient):
    response = client.get("/heroes", params={"role": "invalid"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "ETag" not in response.headers


def test_openapi_schema_is_not_tagged(client: TestClient):
    response = client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    assert "ETag" not in response.headers


@pytest.mark.asyncio
async def test_cache_envelope_carries_the_tag_nginx_will_serve(
    client: TestClient, valkey_server: fakeredis.FakeAsyncRedis
):
    client.get("/heroes")

    stored = await valkey_server.get(f"{settings.api_cache_key_prefix}:/heroes")
    assert isinstance(stored, bytes)
    envelope = json.loads(zstd.decompress(stored).decode("utf-8"))

    assert envelope["etag"] == compute_etag(envelope["data_json"].encode("utf-8"))
