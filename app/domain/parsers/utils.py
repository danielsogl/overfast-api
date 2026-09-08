"""Common utilities for Blizzard HTML/JSON parsing"""

from typing import TYPE_CHECKING
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.config import settings
from app.domain.exceptions import ParserBlizzardError, ParserParsingError
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    import httpx2

_HTTP_504 = 504


def build_blizzard_url(path: str, segment: str) -> str:
    """Build a Blizzard request URL, percent-quoting a user-supplied segment.

    Blizzard IDs are already %7C-encoded (see extract_blizzard_id_from_url), so
    that's normalized back to "|" first -- otherwise quote() would re-escape the
    literal "%" and double-encode it (e.g. "%7C" -> "%257C").
    """
    quoted_segment = quote(segment.replace("%7C", "|"), safe="")
    return f"{settings.blizzard_host}{path}/{quoted_segment}/"


def validate_response_status(
    response: httpx2.Response,
    valid_codes: list[int] | None = None,
) -> None:
    """Validate HTTP response status code.

    Raises:
        ParserBlizzardError: If status code is not in ``valid_codes`` (default: [200])
    """
    if valid_codes is None:
        valid_codes = [200]

    if response.status_code not in valid_codes:
        logger.error(
            "Received an error from Blizzard. HTTP {} : {}",
            response.status_code,
            response.text,
        )
        raise ParserBlizzardError(
            status_code=_HTTP_504,
            message=(
                f"Couldn't get Blizzard page (HTTP {response.status_code} error)"
                f" : {response.text}"
            ),
        )


def parse_html_root(html: str) -> LexborNode:
    """Parse HTML and return the root content node.

    Raises:
        ParserParsingError: If root node not found
    """
    parser = LexborHTMLParser(html)
    root_tag = parser.css_first("div.main-content,main")

    msg = "Could not find main content in HTML"
    if not root_tag:
        raise ParserParsingError(msg)

    return root_tag


def safe_get_text(node: LexborNode | None, default: str = "") -> str:
    """Safely get text from a node, return default if None"""
    return node.text().strip() if node else default


def safe_get_attribute(
    node: LexborNode | None,
    attribute: str,
    default: str = "",
) -> str | None:
    """Safely get attribute from a node, return default if None"""
    if not node or not node.attributes:
        return default
    return node.attributes.get(attribute, default)


def extract_blizzard_id_from_url(url: str) -> str | None:
    """Extract Blizzard ID from career profile URL (keeps URL-encoded format).

    Blizzard redirects BattleTag URLs to Blizzard ID URLs. The ID is kept
    in URL-encoded format to match the format used in search results.

    Examples:
        - Input: /career/df51a381fe20caf8baa7%7C0bf3b4c47cbebe84b8db9c676a4e9c1f/
        - Output: df51a381fe20caf8baa7%7C0bf3b4c47cbebe84b8db9c676a4e9c1f

    Returns:
        Blizzard ID string (URL-encoded), or None if not found
    """
    if "/career/" not in url:
        return None

    try:
        career_segment = url.split("/career/")[1]
        blizzard_id = career_segment.rstrip("/").split("/")[0]

        if not blizzard_id:
            return None
    except IndexError, ValueError:
        logger.warning("Failed to extract Blizzard ID from URL: {}", url)
        return None

    return blizzard_id


def is_blizzard_id(player_id: str) -> bool:
    """Check if a player_id is a Blizzard ID (not a BattleTag).

    Blizzard IDs contain pipe character (| or %7C) and don't have hyphens.
    BattleTags have format: Name-12345

    Examples:
        >>> is_blizzard_id("TeKrop-2217")
        False
        >>> is_blizzard_id("df51a381fe20caf8baa7%7C0bf3b4c47cbebe84b8db9c676a4e9c1f")
        True
        >>> is_blizzard_id("df51a381fe20caf8baa7|0bf3b4c47cbebe84b8db9c676a4e9c1f")
        True
    """
    return ("%7C" in player_id or "|" in player_id) and "-" not in player_id


def normalize_player_id(player_id: str) -> str:
    """The one spelling a Blizzard ID is stored under.

    A Blizzard ID separates its two halves with a pipe, and the same player
    reaches us under both spellings. The search endpoint hands clients the
    percent-encoded ``%7C`` form, and a client that puts that straight into a
    URL path gets it decoded back to ``|`` by the time a handler sees it —
    while one that encodes it again sends ``%257C``, which decodes to a
    literal ``%7C``. Both are the same player and neither is wrong.

    Left alone they become two keys. ``player_snapshots`` collected both, so a
    single player's history was split across two rows sets: the rank
    comparison reads the two newest snapshots *of one key* and therefore
    compared against a stale half, and ``push_subscriptions.player_ids`` held
    both spellings at once, which polled the player twice and sent two
    identical notifications for one rank move.

    Decoded is the canonical form: it is what a handler receives for the
    spelling the search endpoint publishes, so it is the majority already.
    The replacement is spelled out rather than done with ``unquote`` because
    only this one sequence may change — ``unquote`` would also rewrite a
    ``%2D`` a BattleTag is entitled to carry, and is not idempotent here:
    a second pass turns ``%257C`` into a pipe rather than leaving it alone.

    Examples:
        >>> normalize_player_id("abc%7Cdef")
        'abc|def'
        >>> normalize_player_id("abc%7cdef")
        'abc|def'
        >>> normalize_player_id("abc|def")
        'abc|def'
        >>> normalize_player_id("TeKrop-2217")
        'TeKrop-2217'
    """
    return player_id.replace("%7C", "|").replace("%7c", "|")


def match_player_by_blizzard_id(
    search_results: list[dict], blizzard_id: str
) -> dict | None:
    """Match a player from search results by Blizzard ID.

    Used to resolve ambiguous BattleTags when multiple players share the same name.

    Returns:
        Matching player dict, or None if not found
    """
    normalized_blizzard_id = blizzard_id.replace("|", "%7C")  # Normalize for comparison
    for player in search_results:
        if player.get("url") == normalized_blizzard_id:
            return player

    logger.warning(
        "No player found in search results matching Blizzard ID: {}", blizzard_id
    )
    return None
