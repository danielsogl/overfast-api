"""Heroes endpoints router : heroes list, heroes details, etc."""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response, status

from app.api.dependencies import HeroServiceDep
from app.api.enums import RouteTag
from app.api.helpers import (
    apply_swr_headers,
    build_cache_key,
    get_human_readable_duration,
    routes_responses,
)
from app.api.models.heroes import (
    BadRequestErrorMessage,
    Hero,
    HeroParserErrorMessage,
    HeroShort,
    HeroStatsHistory,
    HeroStatsSummary,
)
from app.config import settings
from app.domain.enums import (
    CompetitiveDivisionFilter,
    HeroGamemode,
    HeroKey,
    Locale,
    MapKey,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    Role,
    SubRole,
)

router = APIRouter()


@router.get(
    "",
    responses=routes_responses,
    tags=[RouteTag.HEROES],
    summary="Get a list of heroes",
    description=(
        "Get a list of Overwatch heroes, which can be filtered using roles, subroles or gamemodes. "
        f"<br />**Cache TTL : {get_human_readable_duration(settings.heroes_path_cache_timeout)}.**"
    ),
    operation_id="list_heroes",
    response_model=list[HeroShort],
)
async def list_heroes(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    role: Annotated[Role | SubRole | None, Query(title="Role filter")] = None,
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
    gamemode: Annotated[HeroGamemode | None, Query(title="Gamemode filter")] = None,
) -> Any:
    data, is_stale, age = await service.list_heroes(
        locale=locale, role=role, gamemode=gamemode, cache_key=build_cache_key(request)
    )
    apply_swr_headers(
        response,
        settings.heroes_path_cache_timeout,
        is_stale,
        age,
        staleness_threshold=settings.heroes_staleness_threshold,
    )
    return data


@router.get(
    "/stats",
    responses={
        **routes_responses,
        status.HTTP_400_BAD_REQUEST: {
            "model": BadRequestErrorMessage,
            "description": "Bad Request Error",
        },
    },
    tags=[RouteTag.HEROES],
    summary="Get hero stats",
    description=(
        "Get hero statistics usage, filtered by platform, region, role, etc."
        "Only Role Queue gamemodes are concerned."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_stats_cache_timeout)}.**"
    ),
    operation_id="get_hero_stats",
    response_model=list[HeroStatsSummary],
)
async def get_hero_stats(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    platform: Annotated[
        PlayerPlatform, Query(title="Player platform filter", examples=["pc"])
    ],
    gamemode: Annotated[
        PlayerGamemode,
        Query(
            title="Gamemode",
            description="Filter on a specific gamemode.",
            examples=["competitive"],
        ),
    ],
    region: Annotated[
        PlayerRegion,
        Query(
            title="Region",
            description="Filter on a specific player region.",
            examples=["europe"],
        ),
    ],
    role: Annotated[
        Role | SubRole | None,
        Query(title="Role or subrole filter", examples=["support"]),
    ] = None,
    map_: Annotated[
        MapKey | None, Query(alias="map", title="Map key filter", examples=["hanaoka"])
    ] = None,
    competitive_division: Annotated[
        CompetitiveDivisionFilter | None,
        Query(
            title="Competitive division filter",
            examples=["diamond"],
        ),
    ] = None,
    order_by: Annotated[
        str,
        Query(
            title="Ordering field and the way it's arranged (asc[ending]/desc[ending])",
            pattern=r"^(hero|winrate|pickrate|banrate):(asc|desc)$",
        ),
    ] = "hero:asc",
) -> Any:
    data, is_stale, age = await service.get_hero_stats(
        platform=platform,
        gamemode=gamemode,
        region=region,
        role=role,
        map_filter=map_,
        competitive_division=competitive_division,
        order_by=order_by,
        cache_key=build_cache_key(request),
    )
    apply_swr_headers(
        response,
        settings.hero_stats_cache_timeout,
        is_stale,
        age,
    )
    return data


# Declared before "/{hero_key}" (same reason as "/stats"): otherwise the path
# parameter route would swallow "stats" and 404 on an unknown hero key.
@router.get(
    "/stats/history",
    responses=routes_responses,
    tags=[RouteTag.HEROES],
    summary="Get hero stats history",
    description=(
        "Get the recorded history of hero winrate, pickrate and banrate for one "
        "region, newest first. Blizzard publishes no history of its own : this "
        "API takes one reading a day, so the series starts the day recording "
        "began and has at most one point per day."
        "<br />**Only one slice is recorded : PC, competitive, no role, map or "
        "competitive division filter.** That is why this endpoint has no "
        "`platform` or `gamemode` parameter — recording every combination of the "
        "`/heroes/stats` filters would mean thousands of Blizzard requests a day, "
        "and offering filters the data cannot answer would be worse than not "
        "offering them."
        "<br />An empty `snapshots` list means nothing has been recorded for the "
        "region yet, which is a normal state and not an error."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_stats_cache_timeout)}.**"
    ),
    operation_id="get_hero_stats_history",
    response_model=HeroStatsHistory,
)
async def get_hero_stats_history(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    region: Annotated[
        PlayerRegion,
        Query(
            title="Region",
            description="Region the series was recorded for.",
            examples=["europe"],
        ),
    ],
    hero: Annotated[
        HeroKey | None,
        Query(
            title="Hero filter",
            description=(
                "Return only this hero's series. Days on which the hero was not "
                "recorded are omitted. All heroes are returned by default."
            ),
            examples=["ana"],
        ),
    ] = None,
    since: Annotated[
        int | None,
        Query(
            title="Since",
            description=(
                "Only return readings taken on or after this Unix timestamp "
                "(the day it falls on). All available readings by default."
            ),
            examples=[1739547600],
            gt=0,
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title="Limit",
            description="Maximum number of daily readings to return",
            examples=[30],
            ge=1,
            le=365,
        ),
    ] = 30,
) -> Any:
    data, is_stale, age = await service.get_hero_stats_history(
        region=region,
        hero=str(hero) if hero else None,
        since=since,
        limit=limit,
        cache_key=build_cache_key(request),
    )
    apply_swr_headers(
        response,
        settings.hero_stats_cache_timeout,
        is_stale,
        age,
    )
    return data


@router.get(
    "/{hero_key}",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": HeroParserErrorMessage,
            "description": "Hero Not Found",
        },
        **routes_responses,
    },
    tags=[RouteTag.HEROES],
    summary="Get hero data",
    description=(
        "Get data about an Overwatch hero : description, abilities, stadium powers, story, etc. "
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_path_cache_timeout)}.**"
    ),
    operation_id="get_hero",
    response_model=Hero,
)
async def get_hero(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    hero_key: Annotated[HeroKey, Path(title="Key name of the hero")],
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
) -> Any:
    data, is_stale, age = await service.get_hero(
        hero_key=str(hero_key), locale=locale, cache_key=build_cache_key(request)
    )
    apply_swr_headers(
        response,
        settings.hero_path_cache_timeout,
        is_stale,
        age,
        staleness_threshold=settings.heroes_staleness_threshold,
    )
    return data
