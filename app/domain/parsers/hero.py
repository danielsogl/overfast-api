"""Stateless parser functions for single hero details"""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from app.config import settings
from app.domain.parsers.utils import (
    parse_html_root,
    safe_get_attribute,
    safe_get_text,
    validate_response_status,
)

if TYPE_CHECKING:
    from selectolax.lexbor import LexborNode

    from app.domain.models.hero import (
        Ability,
        AbilityFireMode,
        AbilityVideo,
        HeroBackground,
        HeroDetail,
        Media,
        Perk,
        PerksContainer,
        StadiumPower,
        Story,
        StoryChapter,
    )
    from app.domain.ports import BlizzardClientPort

from app.domain.enums import Locale, MediaType
from app.domain.exceptions import ParserBlizzardError, ParserParsingError
from app.domain.parsers.roles import get_role_from_icon_url
from app.infrastructure.logger import logger


async def fetch_hero_html(
    client: BlizzardClientPort,
    hero_key: str,
    locale: Locale = Locale.ENGLISH_US,
) -> str:
    """Fetch single hero HTML from Blizzard"""
    url = f"{settings.blizzard_host}/{locale}{settings.heroes_path}{hero_key}/"
    response = await client.get(url, headers={"Accept": "text/html"})
    validate_response_status(response, valid_codes=[200, 404])
    return response.text


def parse_hero_html(html: str, locale: Locale = Locale.ENGLISH_US) -> HeroDetail:
    """
    Parse single hero details from HTML

    Args:
        html: Raw HTML content from Blizzard hero page
        locale: Locale for parsing birthday/age text

    Returns:
        Hero dict with name, description, role, location, birthday, age, abilities, story

    Raises:
        ParserBlizzardError: If hero not found (404)
        ParserParsingError: If HTML structure is unexpected
    """
    try:
        root_tag = parse_html_root(html)

        # Check if hero exists
        abilities_section = root_tag.css_first("div.abilities-container")
        if not abilities_section:
            raise ParserBlizzardError(  # noqa: TRY301
                status_code=HTTPStatus.NOT_FOUND.value,
                message="Hero not found or not released yet",
            )

        perks_section = root_tag.css_first("blz-section#perks")
        overview_section = root_tag.css_first("blz-page-header")
        lore_section = root_tag.css_first("blz-section.lore")

        if not overview_section:
            msg = "Hero overview section (blz-page-header) not found"
            raise ParserParsingError(msg)

        # Only the overview is mandatory. Perks and lore are whole sections that
        # a freshly released hero can ship without — the same reason `portrait`
        # is nullable — and losing one of them is no reason to 500 the rest.
        if not perks_section:
            logger.warning("Hero page has no perks section, reporting null perks")
        if not lore_section:
            logger.warning("Hero page has no lore section, reporting null story")

        # Built up as a plain dict — HeroDetail.portrait/hitpoints/stadium_powers
        # are NotRequired, and `**`-merging + conditional assignment onto a
        # TypedDict-annotated dict isn't something the checker follows key by
        # key. cast() below documents the resulting shape without changing
        # anything at runtime (a TypedDict *is* a plain dict).
        hero_data: dict = {
            **_parse_hero_summary(overview_section, locale),
            "abilities": _parse_hero_abilities(abilities_section),
            "perks": _parse_hero_perks(perks_section) if perks_section else None,
            "story": _parse_hero_story(lore_section) if lore_section else None,
        }
        if stadium_wrapper := root_tag.css_first("div.stadium-wrapper"):
            hero_data["stadium_powers"] = _parse_hero_stadium_powers(stadium_wrapper)

    except ParserBlizzardError:
        # Re-raise Blizzard errors (404s) without wrapping
        raise
    except (AttributeError, KeyError, IndexError, TypeError) as error:
        msg = f"Unexpected Blizzard hero page structure: {error}"
        raise ParserParsingError(msg) from error
    else:
        return cast("HeroDetail", hero_data)


def _parse_hero_summary(overview_section: LexborNode, locale: Locale) -> dict:
    """Parse hero summary section (name, role, location, birthday, age)"""
    header_section = overview_section.css_first("blz-header")
    extra_list_items = overview_section.css_first("blz-list").css("blz-list-item")

    birthday_text = safe_get_text(extra_list_items[3].css_first("p"))
    birthday, age = _parse_birthday_and_age(birthday_text, locale)

    role_icon_element = extra_list_items[0].css_first("blz-icon")
    role_icon_url = safe_get_attribute(role_icon_element, "src")

    subrole_icon_element = extra_list_items[1].css_first("blz-icon")
    subrole_icon_url = safe_get_attribute(subrole_icon_element, "src")

    backgrounds: list[HeroBackground] = []
    for img in overview_section.css("blz-image[slot=background]"):
        src = img.attributes.get("src")
        if not src:
            continue
        # str(...) rather than relying on the declared annotation: `x or ""`
        # infers as `str | Literal[""]`, and .split() on that union yields
        # `list[str] | list[LiteralString]` — not the same type as `list[str]`
        # to the checker, even though every value in it is one at runtime.
        bp = str(img.attributes.get("bp") or "")
        backgrounds.append({"url": src, "sizes": bp.split()})

    return {
        "name": safe_get_text(header_section.css_first("h2")),
        "description": safe_get_text(header_section.css_first("p")),
        "backgrounds": backgrounds,
        "role": get_role_from_icon_url(role_icon_url or ""),
        "subrole": get_role_from_icon_url(subrole_icon_url or ""),
        # Blizzard shows the subrole's passive as a tooltip and publishes it
        # nowhere else. Empty string rather than None when absent, matching how
        # safe_get_attribute treats every other optional attribute here.
        "subrole_passive": safe_get_attribute(extra_list_items[1], "descriptiontext")
        or None,
        "location": safe_get_text(extra_list_items[2]),
        "birthday": birthday,
        "age": age,
    }


def _parse_birthday_and_age(text: str, locale: Locale) -> tuple[str | None, int | None]:
    """Extract birthday and age from text for a given locale"""
    birthday_regex = (
        r"^([^\(（]*[^\(（ ])"  # birthday: any chars, must end with non-space/non-paren
        r" [\(（]"  # literal space + open-paren
        r"[^:：]*[:：] ?"  # label + colon separator
        r"(\d+)"  # age digits
        r"[^\)）]*[\)）]$"  # rest + close-paren
    )

    result = re.match(birthday_regex, text)
    if not result:
        return None, None

    # Text for "Unknown" in different locales
    unknown_texts = {
        Locale.GERMAN: "Unbekannt",
        Locale.ENGLISH_EU: "Unknown",
        Locale.ENGLISH_US: "Unknown",
        Locale.SPANISH_EU: "Desconocido",
        Locale.SPANISH_LATIN: "Desconocido",
        Locale.FRENCH: "Inconnu",
        Locale.ITALIANO: "Sconosciuto",
        Locale.JAPANESE: "不明",
        Locale.KOREAN: "알 수 없음",
        Locale.POLISH: "Nieznane",
        Locale.PORTUGUESE_BRAZIL: "Desconhecido",
        Locale.RUSSIAN: "Неизвестно",
        Locale.CHINESE_TAIWAN: "未知",
    }
    unknown_text = unknown_texts.get(locale, "Unknown")

    birthday = result[1] if result[1] != unknown_text else None
    age = int(result[2]) if result[2] else None

    return birthday, age


# Blizzard marks which part of an ability description belongs to primary and
# secondary fire with a mouse-button <img>, and the alt is the same untranslated
# i18n key in every locale — the distinction is conveyed purely visually, in no
# language. `.text()` drops the images, which both loses that information and
# leaves a double space where each one stood.
#
# So the prose stays exactly as Blizzard wrote it (localised, now correctly
# spaced) and the split is reported separately. Injecting "Primary fire:" would
# put an English word inside a Japanese description.
_FIRE_MODE_IMG = re.compile(
    r"<img[^>]*\balt=[\"']overwatch\.page\.herodetail\.ability\."
    r"(?P<mode>primary|secondary)-fire[\"'][^>]*>"
)
_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_ability_description(
    description_node: LexborNode | None,
) -> tuple[str, list[AbilityFireMode]]:
    """Return ``(description, fire_modes)`` for one ability.

    ``fire_modes`` is empty for every ability Blizzard does not mark, which is
    most of them — only weapon abilities carry the mouse-button icons.
    """
    if description_node is None:
        return "", []

    inner = description_node.html or ""
    parts = _FIRE_MODE_IMG.split(inner)
    description = _clean(_TAG.sub(" ", inner))

    # split() yields [before, mode, text, mode, text, ...]; a lone element means
    # no marker was present.
    fire_modes: list[AbilityFireMode] = [
        {"mode": mode, "description": _clean(_TAG.sub(" ", text))}
        for mode, text in zip(parts[1::2], parts[2::2], strict=True)
    ]
    return description, [f for f in fire_modes if f["description"]]


def _parse_hero_abilities(abilities_section: LexborNode) -> list[Ability]:
    """Parse hero abilities section"""
    carousel_section_div = abilities_section.css_first("blz-carousel-section")
    abilities_list_div = carousel_section_div.css_first("blz-carousel")

    # Parse ability descriptions
    abilities_desc = [
        parse_ability_description(desc_div.css_first("p"))
        for desc_div in abilities_list_div.css("blz-feature")
    ]

    # Videos live in the carousel *section*, one DOM level above the abilities
    # themselves, so they cannot be read per ability. Blizzard numbers them with
    # data-group instead, and that number is the ability index — key off it
    # rather than off document order. Enumerating the list positionally means a
    # single extra <blz-web-video> anywhere in the section shifts every ability
    # onto the next one's video: no exception, valid URLs, cached for 24h.
    abilities_videos = _parse_ability_videos(carousel_section_div)

    # Combine into abilities list
    abilities: list[Ability] = []
    tab_controls = abilities_list_div.css_first("blz-tab-controls").css(
        "blz-tab-control"
    )
    for ability_index, ability_div in enumerate(tab_controls):
        abilities.append(
            {
                "name": safe_get_attribute(ability_div, "label") or "",
                "description": abilities_desc[ability_index][0],
                "fire_modes": abilities_desc[ability_index][1],
                "icon": safe_get_attribute(ability_div.css_first("blz-image"), "src")
                or "",
                # None rather than a neighbour's video. A missing video is
                # visibly missing; a wrong one is indistinguishable from a right
                # one, and the old positional index also raised IndexError
                # whenever Blizzard shipped fewer videos than abilities.
                "video": abilities_videos.get(ability_index),
            }
        )

    return abilities


def _parse_ability_videos(carousel_section_div: LexborNode) -> dict[int, AbilityVideo]:
    """Map Blizzard's ``data-group`` ordinal to the video it belongs to.

    A video missing any of its three URLs is dropped: the response model types
    them as URLs, so a partial one fails validation for the whole hero.
    """
    videos: dict[int, AbilityVideo] = {}

    for video_div in carousel_section_div.css("blz-web-video"):
        group = safe_get_attribute(video_div, "data-group")
        if group is None or not group.isdigit():
            logger.warning(
                "Ability video without a usable data-group ({!r}), skipping", group
            )
            continue

        thumbnail = safe_get_attribute(video_div, "poster")
        mp4 = safe_get_attribute(video_div, "mp4")
        webm = safe_get_attribute(video_div, "webm")
        if not (thumbnail and mp4 and webm):
            logger.warning("Incomplete ability video in group {}, skipping", group)
            continue

        videos[int(group)] = {
            "thumbnail": thumbnail,
            "link": {"mp4": mp4, "webm": webm},
        }

    return videos


def _parse_hero_perks(perks_section: LexborNode) -> PerksContainer:
    """Parse hero perks section"""
    return {
        "minor": _parse_perk_level(perks_section.css_first("div.perk-category.minor")),
        "major": _parse_perk_level(perks_section.css_first("div.perk-category.major")),
    }


def _parse_perk_level(perk_level_div: LexborNode) -> list[Perk]:
    return [
        _parse_perk_detail(perk_level_div.css_first("div.perk-details.left")),
        _parse_perk_detail(perk_level_div.css_first("div.perk-details.right")),
    ]


def _parse_perk_detail(perk_detail_div: LexborNode) -> Perk:
    perk_icon = safe_get_attribute(perk_detail_div.css_first("img"), "src") or ""
    perk_info_div = perk_detail_div.css_first("div.perk-info")

    return {
        "name": safe_get_text(perk_info_div.css_first("h3")),
        "description": safe_get_text(perk_info_div.css_first("div[slot=description]")),
        "icon": perk_icon,
    }


def _parse_hero_story(lore_section: LexborNode) -> Story:
    """Parse hero story/lore section"""
    showcase_section = lore_section.css_first("blz-showcase")

    summary_text = safe_get_text(showcase_section.css_first("blz-header p"))
    summary = summary_text.replace("\n", "")

    accordion = lore_section.css_first("blz-accordion-section blz-accordion")

    return {
        "summary": summary,
        "media": _parse_hero_media(showcase_section),
        "chapters": _parse_story_chapters(accordion),
    }


def _parse_hero_media(showcase_section: LexborNode) -> Media | None:
    """Parse hero media (video, comic, or short story)"""
    # Check for YouTube video
    if video := showcase_section.css_first("blz-youtube-video"):
        youtube_id = safe_get_attribute(video, "youtube-id")
        if youtube_id:
            return {
                "type": MediaType.VIDEO,
                "link": f"https://youtu.be/{youtube_id}",
            }

    # Check for button (comic or short story)
    if button := showcase_section.css_first("blz-button"):
        href = safe_get_attribute(button, "href")
        if not href:
            logger.warning("Missing href attribute in button element")
            return None

        analytics_label = safe_get_attribute(button, "analytics-label")
        media_type = (
            MediaType.SHORT_STORY
            if analytics_label == "short-story"
            else MediaType.COMIC
        )

        # Get full URL
        full_url = f"{settings.blizzard_host}{href}" if href.startswith("/") else href

        return {
            "type": media_type,
            "link": full_url,
        }

    return None


def _parse_story_chapters(accordion: LexborNode) -> list[StoryChapter]:
    """Parse hero story chapters from accordion"""
    # Parse chapter content
    chapters_content = [
        " ".join(
            [paragraph.text() for paragraph in content_container.css("p,pr")]
        ).strip()
        for content_container in accordion.css("div[slot=content]")
    ]

    # Parse chapter pictures
    chapters_picture = [
        safe_get_attribute(picture, "src") or ""
        for picture in accordion.css("blz-image")
    ]

    # Parse chapter titles
    titles = [node for node in accordion.iter() if node.tag == "span"]

    return [
        {
            "title": title_span.text().capitalize().strip(),
            "content": chapters_content[title_index],
            "picture": chapters_picture[title_index],
        }
        for title_index, title_span in enumerate(titles)
    ]


def _parse_hero_stadium_powers(stadium_wrapper: LexborNode) -> list[StadiumPower]:
    stadium_carousel = stadium_wrapper.css_first(
        "blz-section#stadium blz-carousel-beta"
    )

    return [
        {
            "name": power.css_first("p.talent-name").text(),
            "description": power.css_first("p.talent-desc").text(),
            "icon": safe_get_attribute(power.css_first("img"), "src") or "",
        }
        for power in stadium_carousel.css("blz-card.talent-card")
    ]
