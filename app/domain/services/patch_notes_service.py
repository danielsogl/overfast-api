"""Patch notes domain service — live patch notes list"""

from app.config import settings
from app.domain.enums import Locale
from app.domain.exceptions import ParserInternalError, ParserParsingError
from app.domain.parsers.patch_notes import (
    fetch_patch_notes_html,
    limit_patch_notes,
    parse_patch_notes_html,
)
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig


class PatchNotesService(StaticDataService):
    """Domain service for Blizzard patch notes."""

    def _patch_notes_config(
        self,
        locale: Locale,
        cache_key: str,
        limit: int | None = None,
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for the patch notes list."""

        async def _fetch() -> str:
            return await fetch_patch_notes_html(self.blizzard_client, locale)

        def _parse(html: str) -> list[dict]:
            try:
                return parse_patch_notes_html(html)
            except ParserParsingError as exc:
                blizzard_url = (
                    f"{settings.blizzard_host}/{locale}{settings.patch_notes_path}"
                )
                raise ParserInternalError(blizzard_url, exc) from exc

        return StaticFetchConfig(
            storage_key=f"patch_notes:{locale}",
            fetcher=_fetch,
            parser=_parse,
            result_filter=(
                (lambda data: limit_patch_notes(data, limit)) if limit else None
            ),
            cache_key=cache_key,
            cache_ttl=settings.patch_notes_cache_timeout,
            staleness_threshold=settings.patch_notes_staleness_threshold,
            entity_type="patch_notes",
        )

    async def list_patch_notes(
        self,
        locale: Locale,
        cache_key: str,
        limit: int | None = None,
    ) -> tuple[list[dict], bool, int]:
        """Return the patch notes list, newest first.

        Stores raw Blizzard HTML per locale so that parser changes take effect
        on the next request after restart.
        """
        return await self.get_or_fetch(
            self._patch_notes_config(locale, cache_key, limit)
        )

    async def refresh_list(self, locale: Locale) -> None:
        """Fetch fresh patch notes, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer.
        """
        locale_str = locale.value
        cache_key = (
            f"/patch-notes?locale={locale_str}"
            if locale != Locale.ENGLISH_US
            else "/patch-notes"
        )
        await self._fetch_and_store(self._patch_notes_config(locale, cache_key))
        await self._invalidate_derived_cache("/patch-notes", keep=cache_key)
