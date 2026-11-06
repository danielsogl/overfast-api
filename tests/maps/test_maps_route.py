from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import status

from app.config import settings
from app.domain.enums import MapGamemode

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_maps(client: TestClient):
    response = client.get("/maps")
    assert response.status_code == status.HTTP_200_OK

    maps = response.json()
    assert len(maps) > 0, "No maps returned"

    for map_data in maps:
        screenshot_url = map_data["screenshot"]
        screenshot_path = screenshot_url.removeprefix(f"{settings.app_base_url}/")
        path = Path(screenshot_path)
        assert path.is_file(), f"Screenshot file does not exist: {path}"


@pytest.mark.parametrize("gamemode", list(MapGamemode))
def test_get_maps_filter_by_gamemode(client: TestClient, gamemode: MapGamemode):
    response = client.get("/maps", params={"gamemode": gamemode})
    assert response.status_code == status.HTTP_200_OK
    assert all(gamemode in map_dict["gamemodes"] for map_dict in response.json())


def test_get_maps_invalid_gamemode(client: TestClient):
    response = client.get("/maps", params={"gamemode": "invalid"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# From upstream's maps.csv (TeKrop/overfast-api#473), plus Serenza, a Push map
# "created for Stadium" per Blizzard's February 2026 patch notes and not in the
# rates page's map dropdown.
@pytest.mark.parametrize(
    ("key", "expected_uses"),
    [
        ("aatlis", ["standard"]),
        ("antarctic-peninsula", ["standard"]),
        ("anubis", ["arcade"]),
        ("arena-victoriae", ["stadium"]),
        ("ayutthaya", ["arcade"]),
        ("black-forest", ["arcade"]),
        ("blizzard-world", ["standard"]),
        ("busan", ["standard", "stadium"]),
        ("castillo", ["arcade"]),
        ("chateau-guillard", ["arcade"]),
        ("circuit-royal", ["standard"]),
        ("colosseo", ["standard", "stadium"]),
        ("dorado", ["standard"]),
        ("ecopoint-antarctica", ["arcade"]),
        ("eichenwalde", ["standard"]),
        ("esperanca", ["standard", "stadium"]),
        ("gogadoro", ["stadium"]),
        ("grimsvotn", ["standard"]),
        ("hanamura", ["arcade"]),
        ("hanaoka", ["arcade"]),
        ("havana", ["standard"]),
        ("hollywood", ["standard"]),
        ("horizon", ["arcade"]),
        ("ilios", ["standard", "stadium"]),
        ("junkertown", ["standard"]),
        ("lijiang-tower", ["standard", "stadium"]),
        ("kanezaka", ["arcade"]),
        ("kings-row", ["standard"]),
        ("malevento", ["arcade"]),
        ("midtown", ["standard"]),
        ("necropolis", ["arcade"]),
        ("nepal", ["standard", "stadium"]),
        ("neon-junction", ["standard"]),
        ("new-junk-city", ["standard"]),
        ("new-queen-street", ["standard"]),
        ("numbani", ["standard"]),
        ("oasis", ["standard", "stadium"]),
        ("paraiso", ["standard"]),
        ("paris", ["arcade"]),
        ("petra", ["arcade"]),
        ("place-lacroix", ["stadium"]),
        ("powder-keg-mine", ["stadium"]),
        ("practice-range", ["training"]),
        ("redwood-dam", ["stadium"]),
        ("rialto", ["standard"]),
        ("route-66", ["standard"]),
        ("runasapi", ["standard"]),
        ("samoa", ["standard", "stadium"]),
        ("shambali-monastery", ["standard"]),
        ("suravasa", ["standard"]),
        ("thames-district", ["stadium"]),
        ("throne-of-anubis", ["arcade"]),
        ("volskaya", ["arcade"]),
        ("watchpoint-gibraltar", ["standard"]),
        ("workshop-chamber", ["workshop"]),
        ("workshop-expanse", ["workshop"]),
        ("workshop-green-screen", ["workshop"]),
        ("workshop-island", ["workshop"]),
        ("wuxing-university", ["stadium"]),
        ("serenza", ["stadium"]),
    ],
)
def test_get_maps_uses(client: TestClient, key: str, expected_uses: list[str]):
    response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    assert next(m for m in response.json() if m["key"] == key)["uses"] == expected_uses


def test_every_map_has_uses(client: TestClient):
    maps = client.get("/maps").json()

    assert all(m["uses"] for m in maps)
