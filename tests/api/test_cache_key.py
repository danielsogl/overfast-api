"""The cache key is a trust boundary: anything a caller can put into it, they
can multiply cache entries with.

Only the app writes this cache; nginx reads it under
``api-cache:<ngx.var.request_uri>``. So a key nginx cannot reconstruct costs the
fast path, never correctness — but a key a *caller* can vary at will costs the
cache itself, because Valkey runs volatile-lru and evicts real entries to make
room for the junk.
"""

from typing import TYPE_CHECKING

import pytest

from app.api import helpers

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.fixture
def observed_keys(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every cache key the app actually computes while serving."""
    seen: list[str] = []
    original = helpers.build_cache_key

    def spy(request):
        key = original(request)
        seen.append(key)
        return key

    for module in ("heroes", "maps", "roles", "gamemodes", "players"):
        target = f"app.api.routers.{module}"
        monkeypatch.setattr(f"{target}.build_cache_key", spy, raising=False)

    return seen


class TestUndeclaredParamsCannotMultiplyEntries:
    def test_junk_params_collapse_onto_one_key(
        self, client: TestClient, observed_keys: list[str]
    ):
        for i in range(5):
            client.get(f"/roles?zzz={i}")

        assert len(set(observed_keys)) == 1, (
            f"each junk value produced its own cache entry: {set(observed_keys)}"
        )

    def test_declared_params_still_separate_entries(
        self, client: TestClient, observed_keys: list[str]
    ):
        """The fix must not collapse parameters that genuinely change the body."""
        client.get("/heroes?role=damage")
        client.get("/heroes?role=tank")

        expected_distinct_keys = 2
        assert len(set(observed_keys)) == expected_distinct_keys, observed_keys

    def test_parameter_order_does_not_split_the_entry(
        self, client: TestClient, observed_keys: list[str]
    ):
        client.get("/heroes?role=damage&gamemode=quickplay")
        client.get("/heroes?gamemode=quickplay&role=damage")

        assert len(set(observed_keys)) == 1, observed_keys

    def test_junk_does_not_change_the_key_of_a_real_request(
        self, client: TestClient, observed_keys: list[str]
    ):
        client.get("/heroes?role=damage")
        client.get("/heroes?role=damage&utm_source=spam")

        assert len(set(observed_keys)) == 1, observed_keys
