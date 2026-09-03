"""Project constants module"""

import tomllib
from functools import cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@cache
def get_app_version() -> str:
    with Path(f"{Path.cwd()}/pyproject.toml").open(mode="rb") as project_file:
        project_data = tomllib.load(project_file)
    return project_data["project"]["version"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ############
    # APPLICATION SETTINGS
    ############

    # Application version, retrieved from pyproject.toml. It should never be
    # overriden in dotenv. Only used in OpenAPI spec and request headers.
    app_version: str = get_app_version()

    # Base URL of the application
    # Used in some endpoints for exposing internal and static links
    # This fork's own deployment. Overridden via APP_BASE_URL; the default
    # matters because it is what the OpenAPI examples and every static asset
    # URL fall back to.
    app_base_url: str = "https://api.mercy-stats.app"

    # Log level for Loguru
    log_level: str = "info"

    # Optional, status page URL if you have any to provide
    status_page_url: str | None = None

    # Profiler to use for debug purposes, disabled by default
    profiler: str | None = None

    # Route path to display as new on the documentation
    new_route_path: str | None = None

    ############
    # PERSISTENT STORAGE CONFIGURATION (PostgreSQL)
    ############

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "overfast"
    postgres_user: str = "overfast"
    postgres_password: str
    postgres_pool_min_size: int = 2
    postgres_pool_max_size: int = 10
    # Seconds before a query is cancelled and its connection returned to the
    # pool. Every statement here is a single-row cache read or write against an
    # indexed key, so 10s is already several orders of magnitude of headroom —
    # anything slower is stuck, not slow.
    postgres_command_timeout: float = 10.0

    @property
    def postgres_dsn(self) -> str:
        """Build asyncpg-compatible DSN from individual connection settings."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Maximum age of player profiles in seconds before they are considered stale.
    # Profiles with updated_at older than this threshold are removed by the periodic
    # background cleanup task to keep the database size bounded. Set to 0 to disable cleanup.
    player_profile_max_age: int = 604800  # 7 days

    # Maximum age of player snapshots in seconds. Unlike profiles, snapshots are
    # the one thing Blizzard cannot hand back — it publishes no history — so this
    # window is what the history and diff endpoints can ever reach back to.
    # Removed by the same daily cleanup task. Set to 0 to keep them forever.
    player_snapshot_max_age: int = 31536000  # 365 days

    # Maximum age of hero stats snapshots in seconds. Three rows a day makes this
    # the smallest table in the system, and its entire value is being long — a
    # winrate series only becomes interesting once it spans several patches — so
    # the window is much wider than the player one. Removed by the same daily
    # cleanup task. Set to 0 to keep them forever.
    hero_stats_snapshot_max_age: int = 63072000  # 2 years

    # Unknown player exponential backoff configuration
    unknown_player_initial_retry: int = 600  # 10 minutes (first check)
    unknown_player_retry_multiplier: int = 3  # retry_after *= 3 each check
    unknown_player_max_retry: int = 21600  # 6 hours cap

    # Minimum check_count to retain a player status entry on shutdown.
    # Entries with check_count strictly below this value (and their associated
    # cooldown keys) are evicted before the Valkey RDB snapshot.
    # Set to 0 to disable this cleanup entirely.
    unknown_player_min_retention_count: int = 5

    ############
    # RATE LIMITING
    ############

    # Name for the response header which will contain
    # the number of seconds before retrying if being rate limited
    retry_after_header: str = "Retry-After"

    # Global rate limit of requests per second per ip to apply on the API
    rate_limit_per_second_per_ip: int = 30

    # Global burst value to apply on rate limit before rejecting requests
    rate_limit_per_ip_burst: int = 5

    # Global maximum number of connection/simultaneous requests per ip
    max_connections_per_ip: int = 10

    ############
    # ADAPTIVE THROTTLING (TCP Slow Start + AIMD)
    ############

    # Enable adaptive throttling for Blizzard requests
    throttle_enabled: bool = True

    # Initial delay between Blizzard requests (seconds)
    throttle_start_delay: float = 2.0

    # Minimum delay / floor (seconds); 0.1 = max 10 req/s
    throttle_min_delay: float = 0.1

    # Maximum delay (cap)
    throttle_max_delay: float = 30.0

    # Number of consecutive 200s required to halve the delay during Slow Start
    throttle_slow_start_n_successes: int = 10

    # Number of consecutive 200s required to decrease delay by delta during AIMD
    throttle_aimd_n_successes: int = 20

    # How much to decrease delay (seconds) per AIMD step
    throttle_aimd_delta: float = 0.05

    # Minimum delay enforced immediately after a Blizzard 403
    throttle_penalty_delay: float = 10.0

    # Seconds after a 403 during which delay cannot decrease (recovery blocked)
    throttle_penalty_duration: int = 60

    ############
    # VALKEY CONFIGURATION
    ############

    # Valkey server host
    valkey_host: str = "127.0.0.1"

    # Valkey server port
    valkey_port: int = 6379

    ############
    # CACHE CONFIGURATION
    ############

    # Name for the response header which will contain
    # the API Cache TTL when calling the API
    cache_ttl_header: str = "X-Cache-TTL"

    # Name of the *request* header a client sends to declare it handles
    # conditional GETs correctly. Only clients sending this get an ETag or a
    # 304 at all; everyone else gets the plain 200 this API always sent
    # before conditional-request support existed.
    #
    # Added after an already-shipped client broke on it. Offering an ETag is
    # enough for a client's *own* HTTP stack to start revalidating, with no
    # cooperation from its application code — and when a stale entry was
    # revalidated, the 304's ``Content-Length: 0`` was merged onto the stored
    # 200 it served back (RFC 9111 4.3.4). That client believed the header
    # over the body it had actually been handed, read nothing, and showed its
    # users an empty screen. Its fetcher was fixed, but a released build can
    # never be patched retroactively: withholding the tag is what keeps one
    # working, because a stored response with no validator cannot be
    # revalidated at all — it is simply refetched in full.
    conditional_get_header: str = "X-Conditional-Get"

    # Prefix for keys in API Cache with entire payload (Valkey).
    # Used by nginx as main API cache.
    api_cache_key_prefix: str = "api-cache"

    # Cache TTL for heroes list data (seconds)
    heroes_path_cache_timeout: int = 86400

    # Cache TTL for specific hero data (seconds)
    hero_path_cache_timeout: int = 86400

    # Cache TTL for local CSV-based data : heroes stats, gamemodes and maps
    csv_cache_timeout: int = 86400

    # Cache TTL for career pages data (seconds)
    career_path_cache_timeout: int = 600

    # Cache TTL for search account data (seconds)
    search_account_path_cache_timeout: int = 600

    ############
    # BATCH ENDPOINTS
    ############

    # How long GET /players/summaries waits for its fan-out before answering
    # with whatever finished and marking the rest pending.
    #
    # Every id is paced through the same Blizzard throttle, and a cold profile
    # costs two round-trips, so a fully cold batch runs to tens of seconds. The
    # ceiling that matters is nginx's proxy_read_timeout (30s): past that the
    # proxy drops the connection and the degraded answer never leaves the
    # building. This value must stay comfortably below it.
    batch_summaries_timeout: float = 10.0

    # Cache TTL for hero stats data (seconds)
    hero_stats_cache_timeout: int = 3600

    # Cache TTL for patch notes (seconds). Patch notes ship a few times a month,
    # but a hotfix lands on the day it is published — a 24h TTL like the other
    # static data would serve yesterday's news, so this follows hero stats at 1h.
    patch_notes_cache_timeout: int = 3600

    ############
    # SWR STALENESS THRESHOLDS
    ############

    # Age (seconds) after which static data is considered stale and triggers
    # a background refresh while still serving the cached response.
    heroes_staleness_threshold: int = 86400  # 24 hours
    maps_staleness_threshold: int = 86400
    gamemodes_staleness_threshold: int = 86400
    roles_staleness_threshold: int = 86400
    # Same reasoning as patch_notes_cache_timeout: news goes stale in hours.
    patch_notes_staleness_threshold: int = 3600  # 1 hour

    # Age (seconds) after which a player profile is considered stale.
    player_staleness_threshold: int = 3600  # 1 hour

    # Age (seconds) beyond which a stored profile is no longer served and the
    # request waits for Blizzard instead.
    #
    # Everything below this is answered from storage immediately, with a
    # background refresh enqueued — that is what stale-while-revalidate means,
    # and it is why a profile nobody has asked about in hours no longer costs
    # its first caller a throttled round-trip.
    #
    # The ceiling exists for one failure mode, not for freshness: if the worker
    # is down, enqueued refreshes never run, and without a bound every profile
    # would keep serving indefinitely old data while looking perfectly healthy.
    # At 24h the system degrades back to slow-but-current instead of fast-and-
    # silently-wrong. Profiles are deleted at player_profile_max_age (7 days)
    # anyway, so this only ever governs the window in between.
    player_max_serve_age: int = 86400  # 24 hours

    # TTL (seconds) for stale responses written to Valkey API cache.
    # Short enough that background refresh (typically seconds) will overwrite it
    # with fresh data before it expires; long enough to absorb burst traffic
    # while the refresh is in-flight.
    stale_cache_timeout: int = 60

    ############
    # UNKNOWN PLAYERS SYSTEM
    ############

    # Indicate if unknown players cache is enabled or not
    unknown_players_cache_enabled: bool = True

    # Prefix for Valkey keys tracking active cooldown windows (has TTL = retry_after)
    unknown_player_cooldown_key_prefix: str = "unknown-player:cooldown"

    # Prefix for Valkey keys storing persistent check count (no TTL, survives cooldown expiry)
    unknown_player_status_key_prefix: str = "unknown-player:status"

    # Prefix for Valkey keys caching the working Blizzard gamemode filter value per gamemode
    gamemode_filter_key_prefix: str = "gamemode-filter"

    ############
    # BACKGROUND WORKER
    ############

    # Maximum number of concurrent worker jobs
    worker_max_concurrent_jobs: int = 10

    # Job timeout in seconds
    worker_job_timeout: int = 300

    ############
    # BLIZZARD
    ############

    # Blizzard base url for Overwatch website
    blizzard_host: str = "https://overwatch.blizzard.com"

    # Blizzard home page with some details
    home_path: str = "/"

    # Route for Overwatch heroes pages (locale can be specified by API users)
    heroes_path: str = "/heroes/"

    # Route for players career pages
    career_path: str = "/en-us/career"

    # Route for searching Overwatch accounts by name
    search_account_path: str = "/en-us/search/account-by-name"

    # Route for retrieving usage statistics about Overwatch heroes
    hero_stats_path: str = "/en-us/rates/data/"

    # Route for the live patch notes page (locale can be specified by API users)
    patch_notes_path: str = "/news/patch-notes/live"

    ############
    # ERROR REPORTING
    ############

    # Error message to be displayed to API users. Critical errors are logged
    # with a full traceback; there is no outbound notification, so the message
    # must not promise one.
    internal_server_error_message: str = (
        "An internal server error occurred during the process. Please open a "
        "GitHub issue if the problem persists : "
        "https://github.com/danielsogl/overfast-api/issues"
    )

    ############
    # LOCAL
    ############

    # Root path for test fixtures, used to update test data when Blizzard pages are updated
    # It should never be overriden in dotenv.
    test_fixtures_root_path: str = f"{Path.cwd()}/tests/fixtures"

    # Root path for Loguru access logs. It should never be overriden in dotenv.
    logs_root_path: str = f"{Path.cwd()}/logs"


@cache
def get_settings() -> Settings:
    # postgres_password has no default (must be set via env/`.env`); ty can't see
    # that BaseSettings populates required fields from the environment at runtime.
    return Settings()


settings = get_settings()
