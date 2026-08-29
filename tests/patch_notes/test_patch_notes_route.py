from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from fastapi import status

from app.config import settings

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_patch_notes(client: TestClient, patch_notes_html_data: str):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=patch_notes_html_data),
    ):
        response = client.get("/patch-notes")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()
    assert response.headers[settings.cache_ttl_header] == str(
        settings.patch_notes_cache_timeout
    )


def test_get_patch_notes_payload_shape(client: TestClient, patch_notes_html_data: str):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=patch_notes_html_data),
    ):
        response = client.get("/patch-notes?limit=1")

    patch_notes = response.json()

    assert len(patch_notes) == 1
    assert set(patch_notes[0]) == {"date", "title", "sections"}
    assert all(
        set(section) == {"title", "kind", "description", "entries"}
        for section in patch_notes[0]["sections"]
    )
    assert all(
        set(entry) == {"title", "hero", "details", "abilities"}
        for section in patch_notes[0]["sections"]
        for entry in section["entries"]
    )


def test_get_patch_notes_with_locale(client: TestClient, patch_notes_html_data: str):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=patch_notes_html_data),
    ) as mock_get:
        response = client.get("/patch-notes?locale=fr-fr")

    assert response.status_code == status.HTTP_200_OK
    assert "/fr-fr/news/patch-notes/live" in mock_get.call_args[0][0]


def test_get_patch_notes_rejects_a_zero_limit(client: TestClient):
    response = client.get("/patch-notes?limit=0")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_patch_notes_blizzard_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get("/patch-notes")

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json() == {
        "error": "Couldn't get Blizzard page (HTTP 503 error) : Service Unavailable",
    }


def test_get_patch_notes_internal_error(client: TestClient):
    with patch(
        "app.domain.services.patch_notes_service.PatchNotesService.list_patch_notes",
        return_value=([{"invalid_key": "invalid_value"}], False, 0),
    ):
        response = client.get("/patch-notes")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"error": settings.internal_server_error_message}
