"""Tests for PatchNotesService — config building, _parse error path and refresh_list"""

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import Locale
from app.domain.exceptions import ParserInternalError, ParserParsingError
from app.domain.services.patch_notes_service import PatchNotesService


def _make_patch_notes_service(storage: AsyncMock | None = None) -> PatchNotesService:
    cache = AsyncMock()
    blizzard_client = AsyncMock()
    task_queue = AsyncMock()
    task_queue.is_job_pending_or_running.return_value = False
    return PatchNotesService(cache, storage or AsyncMock(), blizzard_client, task_queue)


class TestPatchNotesServiceConfig:
    def test_storage_key_and_entity_type_are_locale_scoped(self):
        svc = _make_patch_notes_service()

        config = svc._patch_notes_config(Locale.FRENCH, "/patch-notes?locale=fr-fr")

        assert config.storage_key == "patch_notes:fr-fr"
        assert config.entity_type == "patch_notes"

    def test_no_result_filter_without_a_limit(self):
        svc = _make_patch_notes_service()

        config = svc._patch_notes_config(Locale.ENGLISH_US, "/patch-notes")

        assert config.result_filter is None

    def test_result_filter_applies_the_limit(self):
        svc = _make_patch_notes_service()
        patch_notes = [{"date": "3"}, {"date": "2"}, {"date": "1"}]

        config = svc._patch_notes_config(Locale.ENGLISH_US, "/patch-notes", limit=2)

        assert config.result_filter is not None
        assert config.result_filter(patch_notes) == patch_notes[:2]

    def test_parse_raises_parser_internal_error_on_parser_parsing_error(self):
        svc = _make_patch_notes_service()
        config = svc._patch_notes_config(Locale.ENGLISH_US, "/patch-notes")
        parser = config.parser
        assert parser is not None

        with (
            patch(
                "app.domain.services.patch_notes_service.parse_patch_notes_html",
                side_effect=ParserParsingError("bad HTML"),
            ),
            pytest.raises(ParserInternalError) as exc_info,
        ):
            parser("<bad-html>")

        assert str(Locale.ENGLISH_US) in exc_info.value.blizzard_url


class TestPatchNotesServiceLocalizedHeroKeys:
    @pytest.mark.asyncio
    async def test_english_locale_needs_no_index(self):
        storage = AsyncMock()
        svc = _make_patch_notes_service(storage)

        hero_keys = await svc._localized_hero_keys(Locale.ENGLISH_US)

        assert hero_keys is None
        storage.get_static_data.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_index_is_built_from_the_stored_localised_heroes_list(
        self, heroes_fr_html_data: str
    ):
        storage = AsyncMock()
        storage.get_static_data.return_value = {
            "data": heroes_fr_html_data,
            "updated_at": 0,
        }
        svc = _make_patch_notes_service(storage)

        hero_keys = await svc._localized_hero_keys(Locale.FRENCH)

        assert storage.get_static_data.await_args[0][0] == "heroes:fr-fr"
        assert hero_keys is not None
        assert hero_keys["echo"] == "echo"
        assert hero_keys["chacal"] == "junkrat"

    @pytest.mark.asyncio
    async def test_missing_heroes_list_degrades_to_english_matching(self):
        storage = AsyncMock()
        storage.get_static_data.return_value = None
        svc = _make_patch_notes_service(storage)

        hero_keys = await svc._localized_hero_keys(Locale.FRENCH)

        assert hero_keys is None

    @pytest.mark.asyncio
    async def test_storage_error_degrades_to_english_matching(self):
        storage = AsyncMock()
        storage.get_static_data.side_effect = RuntimeError("storage is down")
        svc = _make_patch_notes_service(storage)

        hero_keys = await svc._localized_hero_keys(Locale.FRENCH)

        assert hero_keys is None


class TestPatchNotesServiceListPatchNotes:
    @pytest.mark.asyncio
    async def test_list_delegates_to_get_or_fetch(self):
        svc = _make_patch_notes_service()
        expected = ([{"date": "2026-08-20"}], False, 0)

        with patch.object(
            svc, "get_or_fetch", new=AsyncMock(return_value=expected)
        ) as mock_get_or_fetch:
            result = await svc.list_patch_notes(
                Locale.ENGLISH_US, "/patch-notes?limit=1", limit=1
            )

        assert result == expected
        assert mock_get_or_fetch.call_args[0][0].cache_key == "/patch-notes?limit=1"


class TestPatchNotesServiceRefreshList:
    @pytest.mark.asyncio
    async def test_refresh_list_calls_fetch_and_store(self):
        svc = _make_patch_notes_service()

        with patch.object(svc, "_fetch_and_store", new=AsyncMock()) as mock_fetch_store:
            await svc.refresh_list(Locale.ENGLISH_US)

        mock_fetch_store.assert_awaited_once()
        config = mock_fetch_store.call_args[0][0]
        assert config.storage_key == "patch_notes:en-us"
        assert config.cache_key == "/patch-notes"

    @pytest.mark.asyncio
    async def test_refresh_list_cache_key_non_english(self):
        svc = _make_patch_notes_service()

        with patch.object(svc, "_fetch_and_store", new=AsyncMock()) as mock_fetch_store:
            await svc.refresh_list(Locale.FRENCH)

        config = mock_fetch_store.call_args[0][0]
        assert config.cache_key == "/patch-notes?locale=fr-fr"

    @pytest.mark.asyncio
    async def test_refresh_list_never_limits_what_it_stores(self):
        svc = _make_patch_notes_service()

        with patch.object(svc, "_fetch_and_store", new=AsyncMock()) as mock_fetch_store:
            await svc.refresh_list(Locale.ENGLISH_US)

        config = mock_fetch_store.call_args[0][0]
        assert config.result_filter is None
