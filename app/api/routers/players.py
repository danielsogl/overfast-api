"""Players endpoints router : players search, players career, statistics, etc."""

import asyncio
import functools
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)

from app.api.dependencies import PlayerServiceDep
from app.api.enums import RouteTag
from app.api.helpers import (
    apply_swr_headers,
    build_cache_key,
    get_human_readable_duration,
)
from app.api.helpers import routes_responses as common_routes_responses
from app.api.models.players import (
    CareerStats,
    Player,
    PlayerCareerStats,
    PlayerHistory,
    PlayerNotFoundError,
    PlayerSearchResult,
    PlayerStatsDiff,
    PlayerStatsSummary,
    PlayerSummaries,
    PlayerSummary,
    PlayerSummaryError,
)
from app.config import settings
from app.domain.enums import (
    HeroKeyCareerFilter,
    PlayerGamemode,
    PlayerPlatform,
)
from app.domain.exceptions import OverfastError, ParserInternalError
from app.infrastructure.logger import logger

# Upper bound on a single fan-out. Each id may cost two throttled Blizzard
# round-trips on a cold profile, so an unbounded list would let one client
# monopolise the throttle for everyone.
MAX_BATCH_SUMMARIES = 20

# ponytail: module constant instead of a Settings field — this router doesn't
# own app/config.py (a parallel change is touching PlayerService). Move this to
# Settings as `batch_summaries_timeout` when that lands.
#
# Every id is serialised through the global adaptive throttle in
# app/adapters/blizzard/throttle.py, so a fully cold MAX_BATCH_SUMMARIES-sized
# batch costs up to 2 round-trips per id, each paced by up to
# throttle_start_delay (2.0s) or, after a 403, throttle_penalty_delay (10.0s)
# — tens of seconds worst case. nginx cuts the whole response at 30s
# (proxy_read_timeout, build/nginx/overfast-api.conf.template). 10s leaves
# ample headroom for serialization/network on top of the budget.
BATCH_SUMMARIES_TIMEOUT_SECONDS = 10.0

# Tasks still in flight when the batch timeout elapses keep running in the
# background so the throttle budget already spent on them isn't wasted and the
# next request finds a warm cache. This set holds a live reference to each one
# so it can't be garbage-collected mid-flight, and is drained by
# _forget_background_task as each finishes.
_BACKGROUND_SUMMARY_TASKS: set[asyncio.Task[Any]] = set()


def _forget_background_task(player_id: str, task: asyncio.Task[Any]) -> None:
    """Drain a background summary fetch once it finishes past the batch budget.

    Retrieves the task's exception (if any) so it never surfaces as an
    "exception was never retrieved" warning, and drops the module-level
    reference that kept it alive.
    """
    _BACKGROUND_SUMMARY_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.opt(exception=exc).warning(
            "Background summary fetch for {} failed after the batch "
            "timeout window had already closed",
            player_id,
        )


# Custom route responses for player careers
career_routes_responses = {
    status.HTTP_404_NOT_FOUND: {
        "model": PlayerNotFoundError,
        "description": "Player Not Found",
    },
    **common_routes_responses,
}


async def get_player_common_parameters(
    player_id: str = Path(
        title="Player unique name",
        description=(
            'Identifier of the player : BattleTag (with "#" replaced by "-"). '
            "You can also put your username if you're the only one using it on Blizzard. "
            "Be careful, letter case (capital/non-capital letters) is important !"
        ),
        examples=["TeKrop-2217", "TeKrop"],
    ),
):
    return {"player_id": player_id}


CommonsPlayerDep = Annotated[dict, Depends(get_player_common_parameters)]


async def get_player_career_common_parameters(
    commons: CommonsPlayerDep,
    gamemode: PlayerGamemode = Query(
        ...,
        title="Gamemode",
        description="Filter on a specific gamemode.",
        examples=["competitive"],
    ),
    platform: PlayerPlatform = Query(
        None,
        title="Platform",
        description=(
            "Filter on a specific platform. If not specified, the only platform the "
            "player played on will be selected. If the player has already played on "
            "both PC and console, the PC stats will be displayed by default."
        ),
        examples=["pc"],
    ),
    hero: HeroKeyCareerFilter = Query(
        None,
        title="Hero key",
        description=(
            "Filter on a specific hero in order to only get his statistics. "
            "You also can specify 'all-heroes' for general stats."
        ),
    ),
):
    return {
        "player_id": commons.get("player_id"),
        "gamemode": gamemode,
        "platform": platform,
        "hero": hero,
    }


CommonsPlayerCareerDep = Annotated[dict, Depends(get_player_career_common_parameters)]

router = APIRouter()


@router.get(
    "",
    responses=common_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Search for a specific player",
    description=(
        "Search for a given player by using its username or BattleTag (with # replaced by -). "
        "<br />You should be able to find the associated player_id to use in order to request career data."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.search_account_path_cache_timeout)}.**"
    ),
    operation_id="search_players",
    response_model=PlayerSearchResult,
)
async def search_players(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    name: Annotated[
        str,
        Query(
            title="Player nickname or BattleTag to search",
            examples=["TeKrop", "TeKrop-2217"],
        ),
    ],
    order_by: Annotated[
        str,
        Query(
            title="Ordering field and the way it's arranged (asc[ending]/desc[ending])",
            pattern=r"^(player_id|name|last_updated_at):(asc|desc)$",
        ),
    ] = "name:asc",
    offset: Annotated[int, Query(title="Offset of the results", ge=0)] = 0,
    limit: Annotated[int, Query(title="Limit of results per page", gt=0)] = 20,
) -> Any:
    cache_key = build_cache_key(request)
    data = await service.search_players(
        name=name,
        order_by=order_by,
        offset=offset,
        limit=limit,
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.search_account_path_cache_timeout, False, 0)
    return data


def parse_player_ids(ids: str) -> list[str]:
    """Split, trim and de-duplicate the comma-separated ``ids`` parameter."""
    player_ids = list(
        dict.fromkeys(
            player_id.strip() for player_id in ids.split(",") if player_id.strip()
        )
    )
    if not player_ids or len(player_ids) > MAX_BATCH_SUMMARIES:
        msg = (
            "The ids parameter must contain between 1 and "
            f"{MAX_BATCH_SUMMARIES} distinct player ids "
            f"(got {len(player_ids)})"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg
        )
    return player_ids


def build_summary_cache_key(player_id: str) -> str:
    """Return the cache key ``GET /players/{player_id}/summary`` would build.

    ``build_cache_key`` keys on the *raw* (percent-encoded) request path, and
    the summary route declares no query parameter, so its key is exactly the
    encoded path. Re-encoding here is what makes a batch call warm the cache
    for later single-player calls and vice versa — without it every player
    would be stored twice.
    """
    return f"/players/{quote(player_id, safe='')}/summary"


def build_summary_error(player_id: str, exc: BaseException) -> PlayerSummaryError:
    """Map a per-player exception to its error entry.

    Mirrors what the API exception handlers would have returned for the
    single-player route : ``OverfastError`` carries its own status and message,
    ``HTTPException`` (Blizzard unavailable / rate limited) its status and
    detail. Anything else is unexpected, and is logged with its traceback
    rather than allowed to fail the whole batch.
    """
    if isinstance(exc, ParserInternalError):
        logger.opt(exception=exc.cause).critical(
            "Internal server error for URL {}", exc.blizzard_url
        )
    elif isinstance(exc, OverfastError):
        return PlayerSummaryError(status_code=exc.status_code, message=exc.message)
    elif isinstance(exc, HTTPException):
        return PlayerSummaryError(status_code=exc.status_code, message=exc.detail)
    else:
        logger.opt(exception=exc).critical(
            "Unexpected error while retrieving summary of {}", player_id
        )

    return PlayerSummaryError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message=settings.internal_server_error_message,
    )


# ROUTE ORDER MATTERS : "/summaries" must stay declared before "/{player_id}",
# otherwise FastAPI matches it as a player id and this endpoint becomes
# unreachable. Same precedent as "/stats" in the heroes router.
@router.get(
    "/summaries",
    responses=common_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get several player summaries at once",
    description=(
        "Get the summaries of up to "
        f"{MAX_BATCH_SUMMARIES} players in a single request, saving a friends "
        "list or a team roster one round-trip per player. "
        "<br />Partial failure is expected : an unknown or unavailable player "
        "fills its own <code>error</code> entry, it never fails the batch. "
        "The batch also answers within a fixed time budget : a player whose "
        "fetch is still running when the budget elapses comes back with "
        "<code>pending</code> set instead of failing the whole request — "
        "retry that id shortly. "
        "Results are returned in the requested order, duplicates removed."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_players_summaries",
    response_model=PlayerSummaries,
)
async def get_players_summaries(
    response: Response,
    service: PlayerServiceDep,
    ids: Annotated[
        str,
        Query(
            title="Player unique names, comma-separated",
            description=(
                "Comma-separated list of player identifiers, each in the same "
                'format as the player_id path parameter : BattleTag (with "#" '
                'replaced by "-") or Blizzard ID. Between 1 and '
                f"{MAX_BATCH_SUMMARIES} distinct ids, duplicates are collapsed."
            ),
            examples=["TeKrop-2217,KIRIKO-12460"],
        ),
    ],
) -> Any:
    player_ids = parse_player_ids(ids)

    # The single-flight lock in PlayerService already collapses concurrent work
    # for the same player, so a shared id costs one Blizzard round-trip.
    # ensure_future (not gather) so each fetch is its own Task we keep a
    # reference to past the timeout below — asyncio.wait never cancels what it
    # doesn't finish waiting for, unlike gather under wait_for.
    tasks = {
        player_id: asyncio.ensure_future(
            service.get_player_summary(
                player_id=player_id,
                cache_key=build_summary_cache_key(player_id),
            )
        )
        for player_id in player_ids
    }

    _done, pending = await asyncio.wait(
        tasks.values(), timeout=BATCH_SUMMARIES_TIMEOUT_SECONDS
    )

    # Whatever didn't finish in time keeps running in the background instead
    # of being cancelled : it's mid-way through Blizzard fetches that will
    # still populate storage and the API cache, and killing it would waste the
    # throttle budget already spent for nothing.
    for player_id, task in tasks.items():
        if task in pending:
            _BACKGROUND_SUMMARY_TASKS.add(task)
            task.add_done_callback(
                functools.partial(_forget_background_task, player_id)
            )

    results = []
    batch_is_stale = False
    batch_age = 0
    for player_id, task in tasks.items():
        if task in pending:
            results.append(
                {
                    "player_id": player_id,
                    "summary": None,
                    "error": None,
                    "pending": True,
                }
            )
            continue

        exc = task.exception()
        if exc is not None:
            results.append(
                {
                    "player_id": player_id,
                    "summary": None,
                    "error": build_summary_error(player_id, exc),
                    "pending": False,
                }
            )
            continue

        summary, is_stale, age = task.result()
        results.append(
            {
                "player_id": player_id,
                "summary": summary,
                "error": None,
                "pending": False,
            }
        )
        batch_is_stale = batch_is_stale or is_stale
        batch_age = max(batch_age, age)

    # ponytail: the batch response is deliberately not cached under its own key.
    # Every entry is already cached individually by get_player_summary, and a
    # second copy keyed on the id-list would give each distinct friend-list
    # permutation its own Valkey entry — cache pollution under volatile-lru.
    apply_swr_headers(
        response, settings.career_path_cache_timeout, batch_is_stale, batch_age
    )
    return {"results": results}


@router.get(
    "/{player_id}/summary",
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player summary",
    description=(
        "Get player summary : name, avatar, competitive ranks, etc. "
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_summary",
    response_model=PlayerSummary,
)
async def get_player_summary(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerDep,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_summary(
        player_id=commons["player_id"],
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}/stats/summary",
    response_model_exclude_unset=True,
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player stats summary",
    description=(
        "Get player statistics summary, with stats usually used for tracking "
        "progress : winrate, kda, damage, healing, etc. "
        "<br /> Data is regrouped in 3 sections : general (sum of all stats), "
        "roles (sum of stats for each role) and heroes (stats for each hero)."
        "<br /> Depending on filters, data from both competitive and quickplay, "
        "and/or pc and console will be merged."
        "<br />Default behaviour : all gamemodes and platforms are taken in account."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_stats_summary",
    response_model=PlayerStatsSummary,
)
async def get_player_stats_summary(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerDep,
    gamemode: Annotated[
        PlayerGamemode | None,
        Query(
            title="Gamemode",
            description=(
                "Filter on a specific gamemode. If not specified, the data of "
                "every gamemode will be combined."
            ),
            examples=["competitive"],
        ),
    ] = None,
    platform: Annotated[
        PlayerPlatform | None,
        Query(
            title="Platform",
            description=(
                "Filter on a specific platform. If not specified, the data of "
                "every platform will be combined."
            ),
            examples=["pc"],
        ),
    ] = None,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_stats_summary(
        player_id=commons["player_id"],
        gamemode=gamemode,
        platform=platform,
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}/stats/career",
    response_model_exclude_unset=True,
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player career stats",
    description=(
        "Career contains numerous statistics grouped by heroes and categories "
        "(combat, game, best, hero specific, average, etc.). Filter them on "
        "specific platform and gamemode (mandatory). You can even retrieve "
        "data about a specific hero of your choice."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_career_stats",
    response_model=PlayerCareerStats,
)
async def get_player_career_stats(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerCareerDep,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_career_stats(
        player_id=commons["player_id"],
        gamemode=commons["gamemode"],
        platform=commons.get("platform"),
        hero=commons.get("hero"),
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}/stats",
    response_model_exclude_unset=True,
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player stats with labels",
    description=(
        "This endpoint exposes the same data as the previous one, except it also "
        "exposes labels of the categories and statistics."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_stats",
    response_model=CareerStats,
)
async def get_player_stats(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerCareerDep,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_stats(
        player_id=commons["player_id"],
        gamemode=commons["gamemode"],
        platform=commons.get("platform"),
        hero=commons.get("hero"),
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}/history",
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player snapshot history",
    description=(
        "Get the recorded history of a player profile, newest first. Blizzard "
        "publishes no history of its own : each snapshot is a small record kept "
        "every time this API served a new version of the profile, so the series "
        "starts the first time the player was requested here."
        "<br />Requesting this endpoint also refreshes the profile, so the "
        "current state is always part of the series."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_history",
    response_model=PlayerHistory,
)
async def get_player_history(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerDep,
    since: Annotated[
        int | None,
        Query(
            title="Since",
            description=(
                "Only return snapshots recorded at or after this Unix timestamp. "
                "All available snapshots are returned by default."
            ),
            examples=[1739547600],
            gt=0,
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title="Limit",
            description="Maximum number of snapshots to return",
            examples=[100],
            ge=1,
            le=500,
        ),
    ] = 100,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_history(
        player_id=commons["player_id"],
        cache_key=cache_key,
        since=since,
        limit=limit,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}/stats/diff",
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get player progress over a period",
    description=(
        "Compare the oldest recorded snapshot in the requested window against "
        "the most recent one : rank movements, per-hero playtime and wins gained, "
        "and the totals across heroes."
        "<br />A player with fewer than two snapshots in the window gets "
        "`snapshots_compared` below 2 and empty deltas — that is a normal "
        '"nothing recorded yet" state, not an error.'
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_stats_diff",
    response_model=PlayerStatsDiff,
)
async def get_player_stats_diff(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerDep,
    since: Annotated[
        int | None,
        Query(
            title="Since",
            description=(
                "Unix timestamp the comparison window starts at. "
                "Defaults to 24 hours before the request."
            ),
            examples=[1739547600],
            gt=0,
        ),
    ] = None,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_stats_diff(
        player_id=commons["player_id"],
        cache_key=cache_key,
        since=since,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/{player_id}",
    responses=career_routes_responses,
    tags=[RouteTag.PLAYERS],
    summary="Get all player data",
    description=(
        "Get all player data : summary and statistics with labels."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.career_path_cache_timeout)}.**"
    ),
    operation_id="get_player_career",
    response_model=Player,
)
async def get_player_career(
    request: Request,
    response: Response,
    service: PlayerServiceDep,
    commons: CommonsPlayerDep,
    gamemode: Annotated[
        PlayerGamemode | None,
        Query(
            title="Gamemode",
            description="Filter on a specific gamemode. All gamemodes are displayed by default.",
            examples=["competitive"],
        ),
    ] = None,
    platform: Annotated[
        PlayerPlatform | None,
        Query(
            title="Platform",
            description="Filter on a specific platform. All platforms are displayed by default.",
            examples=["pc"],
        ),
    ] = None,
) -> Any:
    cache_key = build_cache_key(request)
    data, is_stale, age = await service.get_player_career(
        player_id=commons["player_id"],
        gamemode=gamemode,
        platform=platform,
        cache_key=cache_key,
    )
    apply_swr_headers(response, settings.career_path_cache_timeout, is_stale, age)
    return data
