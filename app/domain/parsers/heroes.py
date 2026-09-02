"""Stateless parser functions for Blizzard heroes data"""

from typing import TYPE_CHECKING

from app.config import settings
from app.domain.enums import HeroGamemode, HeroKey, Locale
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.utils import (
    parse_html_root,
    safe_get_attribute,
    safe_get_text,
    validate_response_status,
)
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.models.hero import HeroListEntry
    from app.domain.ports import BlizzardClientPort


async def fetch_heroes_html(
    client: BlizzardClientPort,
    locale: Locale = Locale.ENGLISH_US,
) -> str:
    """
    Fetch heroes list HTML from Blizzard

    Raises:
        HTTPException: If Blizzard returns non-200 status
    """

    url = f"{settings.blizzard_host}/{locale}{settings.heroes_path}"
    response = await client.get(url, headers={"Accept": "text/html"})
    validate_response_status(response)
    return response.text


def parse_heroes_html(html: str) -> list[HeroListEntry]:
    """
    Parse heroes list HTML into structured data

    Args:
        html: Raw HTML content from Blizzard heroes page

    Returns:
        List of hero dicts with keys: key, name, portrait, role (sorted by key)

    Raises:
        ParserParsingError: If parsing fails
    """
    try:
        root_tag = parse_html_root(html)

        heroes: list[HeroListEntry] = []
        for hero_element in root_tag.css("div.heroIndexWrapper blz-media-gallery a"):
            hero_url = safe_get_attribute(hero_element, "href")
            if not hero_url:
                msg = "Invalid hero URL"
                raise ParserParsingError(msg)

            name_element = hero_element.css_first("blz-card blz-content-block h2")
            portrait_element = hero_element.css_first("blz-card blz-image")

            # See app/domain/models/hero.py's module docstring: these are real
            # HeroGamemode instances now, but come back as plain str once
            # round-tripped through JSONB storage — the field type on
            # HeroListEntry accounts for both.
            gamemodes: list[HeroGamemode | str] = [HeroGamemode.QUICKPLAY]
            if hero_element.css_matches("blz-card blz-badge.stadium-badge"):
                gamemodes.append(HeroGamemode.STADIUM)

            hero_key = hero_url.split("/")[-1]
            if hero_key not in HeroKey:
                # HeroShort.key is typed HeroKey, so one hero Blizzard ships
                # before we add the row failed response validation and took the
                # WHOLE endpoint down with it — /heroes 500ing entirely because
                # of a single unknown key.
                #
                # Serving the other 53 is strictly better, and this does not go
                # unnoticed: scripts/check_blizzard_drift.py compares the live
                # index against the enum daily and fails on exactly this.
                logger.warning(
                    "Unknown hero {!r} on the Blizzard index — not in heroes.csv "
                    "yet, skipping it rather than failing /heroes",
                    hero_key,
                )
                continue

            heroes.append(
                {
                    "key": hero_key,
                    "name": safe_get_text(name_element),
                    "portrait": safe_get_attribute(portrait_element, "src") or "",
                    "role": safe_get_attribute(hero_element, "data-role") or "",
                    "subrole": safe_get_attribute(hero_element, "data-subrole") or "",
                    "gamemodes": gamemodes,
                    # Blizzard's own "new hero" marker, which it drops again a
                    # season or two after release. Absent on every other card.
                    "is_new": hero_element.attributes.get("data-new") == "true",
                }
            )

        return sorted(heroes, key=lambda hero: hero["key"])

    except (AttributeError, KeyError, IndexError, TypeError) as error:
        error_msg = f"Failed to parse heroes HTML: {error!r}"
        raise ParserParsingError(error_msg) from error


def filter_heroes(
    heroes: list[HeroListEntry], role: str | None, gamemode: HeroGamemode | None
) -> list[HeroListEntry]:
    """Filter heroes list by role and gamemode"""
    if role:
        heroes = [
            hero for hero in heroes if hero["role"] == role or hero["subrole"] == role
        ]

    if gamemode:
        heroes = [hero for hero in heroes if gamemode in hero["gamemodes"]]

    return heroes
