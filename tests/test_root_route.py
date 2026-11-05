from typing import TYPE_CHECKING

import pytest
from fastapi import status

from app.main import app

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_root_reports_ok_for_health_checks(client: TestClient):
    response = client.get("/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_is_not_served(client: TestClient, path: str):
    assert client.get(path).status_code == status.HTTP_404_NOT_FOUND


def test_schema_stays_exportable():
    assert "/heroes" in app.openapi()["paths"]
