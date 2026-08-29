"""Patch notes endpoints router : live patch notes list"""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from app.api.dependencies import PatchNotesServiceDep
from app.api.enums import RouteTag
from app.api.helpers import (
    apply_swr_headers,
    build_cache_key,
    get_human_readable_duration,
    routes_responses,
)
from app.api.models.patch_notes import PatchNote
from app.config import settings
from app.domain.enums import Locale

router = APIRouter()


@router.get(
    "",
    responses=routes_responses,
    tags=[RouteTag.PATCH_NOTES],
    summary="Get the latest Overwatch patch notes",
    description=(
        "Get the patches Blizzard currently publishes on its live patch notes "
        "page, newest first. Hero updates carry the matching hero key when it "
        "resolves, so they can be joined with a player's most played heroes."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.patch_notes_cache_timeout)}.**"
    ),
    operation_id="list_patch_notes",
    response_model=list[PatchNote],
)
async def list_patch_notes(
    request: Request,
    response: Response,
    service: PatchNotesServiceDep,
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
    limit: Annotated[
        int | None,
        Query(
            title="Maximum number of patches to return",
            description="Keep only the N most recent patches.",
            ge=1,
        ),
    ] = None,
) -> Any:
    data, is_stale, age = await service.list_patch_notes(
        locale=locale, cache_key=build_cache_key(request), limit=limit
    )
    apply_swr_headers(
        response,
        settings.patch_notes_cache_timeout,
        is_stale,
        age,
        staleness_threshold=settings.patch_notes_staleness_threshold,
    )
    return data
