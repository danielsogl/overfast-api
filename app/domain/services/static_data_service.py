import inspect
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.domain.parsers import PARSER_VERSION
from app.domain.ports.storage import StaticDataCategory
from app.domain.services import BaseService
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class StaticFetchConfig:
    """Parameter object grouping all inputs needed for a static SWR fetch.

    Pass a single ``StaticFetchConfig`` to ``StaticDataService.get_or_fetch``
    instead of passing each field as a separate keyword argument.
    """

    storage_key: str
    fetcher: Callable[[], Any]
    cache_key: str
    cache_ttl: int
    staleness_threshold: int
    entity_type: str
    parser: Callable[[Any], Any] | None = field(default=None)
    result_filter: Callable[[Any], Any] | None = field(default=None)


class StaticDataService(BaseService):
    """SWR orchestration for static content backed by the ``static_data`` persistent storage table.

    Staleness is determined by a configurable time threshold.  Concrete static
    services (heroes, maps, gamemodes, roles) call ``get_or_fetch`` with a
    ``StaticFetchConfig`` — no subclass-level overrides are needed for the
    storage layer.

    Note: Valkey API-cache *reads* happen at the Nginx/Lua layer before FastAPI
    is reached; this service only ever *writes* to the API cache.
    """

    async def get_or_fetch(self, config: StaticFetchConfig) -> tuple[Any, bool, int]:
        """SWR orchestration for static data.

        Returns:
            ``(data, is_stale, age_seconds)`` tuple.  ``age_seconds`` is the
            number of seconds since the data was last stored in persistent storage (0 on
            a cold-start fetch).
        """
        stored = await self._load_from_storage(config.storage_key)
        if stored is not None:
            return await self._serve_from_storage(stored, config)

        return await self._cold_fetch(config)

    async def _load_from_storage(self, storage_key: str) -> dict[str, Any] | None:
        """Load raw + parsed source from the ``static_data`` table. Returns ``None`` on miss."""
        result = await self.storage.get_static_data(storage_key)
        return (
            {
                "raw": result["data"],
                "parsed": result.get("parsed"),
                "data_version": result.get("data_version"),
                "updated_at": result["updated_at"],
            }
            if result
            else None
        )

    async def _serve_from_storage(
        self, stored: dict[str, Any], config: StaticFetchConfig
    ) -> tuple[Any, bool, int]:
        """Serve data from a persistent storage hit, triggering a background refresh if stale.

        For Blizzard HTML sources, the stored ``parsed`` payload is used directly
        when it is stamped with the current ``PARSER_VERSION`` — no re-parse.
        Otherwise (missing, or written by an older parser) the raw source is
        parsed once here and the result is written back, so a parser code
        change takes effect on the very next request without paying the parse
        cost on every subsequent miss. CSV sources are always re-read from the
        local file (see ``_parse_stored``) so that CSV edits take effect
        immediately.
        """
        data = await self._get_parsed(stored, config)
        age = int(time.time()) - stored["updated_at"]
        is_stale = age >= config.staleness_threshold
        filtered = self._apply_filter(data, config.result_filter)

        if is_stale:
            logger.info(
                "[SWR] {} stale (age={}s, threshold={}s) — serving + triggering refresh",
                config.entity_type,
                age,
                config.staleness_threshold,
            )
            await self._enqueue_refresh(
                config.entity_type,
                config.storage_key,
            )
            # Preserve the original stored_at so Age is computed correctly by nginx/Lua.
            # Use the full cache_ttl (not stale_cache_timeout) so X-Cache-TTL reflects the
            # real remaining lifetime of the entry, not just the short SWR window.
            await self._update_api_cache(
                config.cache_key,
                filtered,
                config.cache_ttl,
                stored_at=stored["updated_at"],
                staleness_threshold=config.staleness_threshold,
                stale_while_revalidate=settings.stale_cache_timeout,
            )
        else:
            logger.info(
                "[SWR] {} fresh (age={}s) — serving from persistent storage",
                config.entity_type,
                age,
            )
            # Preserve the original stored_at so Age is computed correctly by nginx/Lua.
            # Without this, every Valkey re-write resets stored_at to now, making Age ≈ 0.
            await self._update_api_cache(
                config.cache_key,
                filtered,
                config.cache_ttl,
                stored_at=stored["updated_at"],
                staleness_threshold=config.staleness_threshold,
            )

        return filtered, is_stale, age

    async def _parse_stored(self, raw: str, config: StaticFetchConfig) -> Any:
        """Produce structured data from ``raw`` stored source.

        - If ``config.parser`` is set: the stored ``raw`` is HTML (or a JSON-encoded
          multi-source dict); apply the parser directly.
        - If ``config.parser`` is not set: the source is a CSV file; re-call
          ``fetcher()`` to get always-current data (fast local I/O).
        """
        if config.parser is not None:
            return config.parser(raw)

        # CSV sources: re-read from file rather than using the stored JSON.
        if inspect.iscoroutinefunction(config.fetcher):
            return await config.fetcher()
        return config.fetcher()

    async def _get_parsed(
        self, stored: dict[str, Any], config: StaticFetchConfig
    ) -> Any:
        """Return structured data for a storage hit, reusing the stored parse when valid.

        CSV sources (``config.parser is None``) have nothing to gain from a
        stored parsed copy of a local file read — always re-read via
        ``_parse_stored`` so CSV edits take effect immediately (see its
        docstring).

        HTML/JSON sources use ``stored["parsed"]`` as-is when present and its
        ``data_version`` matches the current ``PARSER_VERSION``. Otherwise the
        raw source is parsed once and the result is written back so the next
        hit skips the parse too.
        """
        if config.parser is None:
            return await self._parse_stored(stored["raw"], config)

        if stored["parsed"] is not None and stored["data_version"] == PARSER_VERSION:
            return stored["parsed"]

        parsed = await self._parse_stored(stored["raw"], config)
        await self._write_back_parsed(config.storage_key, parsed)
        return parsed

    async def _write_back_parsed(self, storage_key: str, parsed: Any) -> None:
        """Persist a freshly (re)parsed payload so future storage hits skip the parse.

        Same posture as ``_store_in_storage``: a write failure here must not
        fail the request that already has ``parsed`` in hand — log and move on.
        Deliberately does not touch ``updated_at`` (``set_static_data_parsed``
        never does — see the port docstring).
        """
        try:
            await self.storage.set_static_data_parsed(
                key=storage_key,
                parsed=parsed,
                data_version=PARSER_VERSION,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SWR] Parsed write-back failed for {}: {}", storage_key, exc
            )

    @staticmethod
    def _apply_filter(data: Any, result_filter: Callable[[Any], Any] | None) -> Any:
        """Apply ``result_filter`` to ``data`` if provided, otherwise return as-is."""
        return result_filter(data) if result_filter is not None else data

    async def _fetch_and_store(self, config: StaticFetchConfig) -> Any:
        """Fetch from source, persist raw source to persistent storage, update Valkey, return filtered data."""
        if inspect.iscoroutinefunction(config.fetcher):
            raw = await config.fetcher()
        else:
            raw = config.fetcher()

        data = config.parser(raw) if config.parser is not None else raw

        # Store the raw source so re-parses on storage hits always use current parser code.
        # For HTML sources (parser set): raw is the HTML string.
        # For CSV sources (no parser): raw is already the parsed data; serialise as JSON.
        raw_to_store = (
            raw if config.parser is not None else json.dumps(raw, separators=(",", ":"))
        )
        # CSV sources get no stored parse: ``_get_parsed`` always re-reads the
        # local file for them, so a parsed copy would be a second identical blob
        # written on every refresh and read by nobody — and the next person to
        # find a populated ``parsed`` column that nothing consults would have to
        # work out why. Only HTML/JSON sources have a parse worth keeping.
        await self._store_in_storage(
            config.storage_key,
            raw_to_store,
            config.entity_type,
            parsed=data if config.parser is not None else None,
        )

        filtered = self._apply_filter(data, config.result_filter)
        await self._update_api_cache(
            config.cache_key,
            filtered,
            config.cache_ttl,
            staleness_threshold=config.staleness_threshold,
        )

        return filtered

    async def _cold_fetch(self, config: StaticFetchConfig) -> tuple[Any, bool, int]:
        """Fetch from source on cold start, persist to storage and Valkey."""
        logger.info(
            "[SWR] {} not in storage — fetching from source", config.entity_type
        )
        filtered = await self._fetch_and_store(config)
        return filtered, False, 0

    async def _store_in_storage(
        self,
        storage_key: str,
        raw: str,
        entity_type: str,
        parsed: Any = None,
    ) -> None:
        """Persist raw source string + its parsed form to the ``static_data`` table.

        ``raw`` is zstd-compressed BYTEA; ``parsed`` (JSONB) is stamped with the
        current ``PARSER_VERSION`` so a later storage hit can skip re-parsing.
        """
        try:
            await self.storage.set_static_data(
                key=storage_key,
                data=raw,
                category=StaticDataCategory(entity_type),
                data_version=PARSER_VERSION,
                parsed=parsed,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SWR] Storage write failed for {}: {}", storage_key, exc)
