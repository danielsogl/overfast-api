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


def _hero_keys_by_title(patch_notes: list[dict]) -> dict[str, str | None]:
    return {
        entry["title"]: entry["hero"]
        for patch in patch_notes
        for section in patch["sections"]
        for entry in section["entries"]
        if section["kind"] == "hero_update"
    }


def test_get_patch_notes_resolves_localised_hero_names(
    client: TestClient,
    patch_notes_fr_html_data: str,
    heroes_fr_html_data: str,
):
    # Populate the fr-fr heroes list the way the API itself does, so the patch
    # notes request finds it in storage under "heroes:fr-fr".
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=heroes_fr_html_data),
    ):
        client.get("/heroes?locale=fr-fr")

    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_200_OK, text=patch_notes_fr_html_data
        ),
    ):
        response = client.get("/patch-notes?locale=fr-fr")

    heroes = _hero_keys_by_title(response.json())

    assert response.status_code == status.HTTP_200_OK
    assert heroes["Écho"] == "echo"
    assert heroes["Chacal"] == "junkrat"
    assert heroes["Danger"] == "hazard"
    assert heroes["Vital"] == "lifeweaver"


def test_get_patch_notes_without_a_localised_heroes_list_keeps_raw_names(
    client: TestClient,
    patch_notes_fr_html_data: str,
):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_200_OK, text=patch_notes_fr_html_data
        ),
    ):
        response = client.get("/patch-notes?locale=fr-fr")

    heroes = _hero_keys_by_title(response.json())

    assert response.status_code == status.HTTP_200_OK
    assert heroes["Écho"] is None
    assert heroes["Chacal"] is None
    # English-identical names still resolve through the heroes.csv fallback.
    assert heroes["Ana"] == "ana"


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
