"""Patch notes domain service — live patch notes list"""

from app.config import settings
from app.domain.enums import Locale
from app.domain.exceptions import ParserInternalError, ParserParsingError
from app.domain.parsers.heroes import parse_heroes_html
from app.domain.parsers.patch_notes import (
    fetch_patch_notes_html,
    limit_patch_notes,
    parse_patch_notes_html,
)
from app.domain.services import SwrResult
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig
from app.domain.utils.helpers import build_hero_key_index
from app.infrastructure.logger import logger


class PatchNotesService(StaticDataService):
    """Domain service for Blizzard patch notes."""

    async def _localized_hero_keys(self, locale: Locale) -> dict[str, str] | None:
        """Return the hero name → key index for *locale*, or ``None``.

        Patch notes are localised and heroes.csv is English-only, so localised
        names ("Écho", "Chacal") never resolve against it. Blizzard's own heroes
        list *is* scraped per locale and already sitting in storage under
        ``heroes:{locale}`` — that is where the localised names come from.

        ``None`` means "match against the English heroes.csv": either the locale
        already is English, or its heroes list has never been scraped. Degrading
        is the point — a missing heroes list must not fail a patch notes request.

        ponytail: one extra storage read plus a heroes-page parse per localised
        request. Memoise per locale if that ever shows up in a profile.
        """
        if locale == Locale.ENGLISH_US:
            return None

        try:
            stored = await self._load_from_storage(f"heroes:{locale}")
            heroes = parse_heroes_html(stored["raw"]) if stored else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[patch notes] Unusable {} heroes list ({}) — falling back to "
                "English hero name matching",
                locale,
                exc,
            )
            return None

        if not heroes:
            logger.warning(
                "[patch notes] No stored {} heroes list — falling back to English "
                "hero name matching",
                locale,
            )
            return None

        return build_hero_key_index(heroes)

    def _patch_notes_config(
        self,
        locale: Locale,
        cache_key: str,
        limit: int | None = None,
        hero_keys: dict[str, str] | None = None,
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for the patch notes list."""

        async def _fetch() -> str:
            return await fetch_patch_notes_html(self.blizzard_client, locale)

        def _parse(html: str) -> list[dict]:
            try:
                return parse_patch_notes_html(html, hero_keys)
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
    ) -> SwrResult[list[dict]]:
        """Return the patch notes list, newest first.

        Stores raw Blizzard HTML per locale so that parser changes take effect
        on the next request after restart.
        """
        hero_keys = await self._localized_hero_keys(locale)
        return await self.get_or_fetch(
            self._patch_notes_config(locale, cache_key, limit, hero_keys)
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
        hero_keys = await self._localized_hero_keys(locale)
        await self._fetch_and_store(
            self._patch_notes_config(locale, cache_key, hero_keys=hero_keys)
        )
        await self._invalidate_derived_cache("/patch-notes", keep=cache_key)
