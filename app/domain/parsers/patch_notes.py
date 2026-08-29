"""Stateless parser functions for Blizzard patch notes.

The live page (``/{locale}/news/patch-notes/live``) is server-rendered and, for
once, generously classed: every patch is a ``.PatchNotes-patch``, every section
carries its own kind in a ``PatchNotes-section-<kind>`` class, and hero updates
expose their abilities individually. There is nothing to reconstruct from prose.

Two deliberate choices:

- ``date`` comes from the ``.anchor`` id (``patch-2026-08-14``), not from the
  ``.PatchNotes-date`` text. The text is localised ("14 août 2026"); the anchor
  id is the same ISO date in every locale.
- Section titles are Blizzard's own ("Hero Updates", "Bug Fixes", "Busan -
  Control"). We do not map them onto a taxonomy of our own — they change with
  every patch and any mapping would rot silently.

Hero names are localised too ("Écho", "Chacal"), and heroes.csv is English-only.
The caller may therefore hand in ``hero_keys``: a normalised name → hero key
index built from Blizzard's own localised heroes list. This module stays
stateless — building that index needs storage, which is the service's job.
"""

from typing import TYPE_CHECKING

from app.config import settings
from app.domain.enums import Locale
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.utils import (
    parse_html_root,
    safe_get_text,
    validate_response_status,
)
from app.domain.utils.helpers import get_hero_key, normalize_hero_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from selectolax.lexbor import LexborNode

    from app.domain.ports import BlizzardClientPort

# Blizzard's own section kinds, taken from the "PatchNotes-section-<kind>" class.
_SECTION_CLASS_PREFIX = "PatchNotes-section-"


async def fetch_patch_notes_html(
    client: BlizzardClientPort,
    locale: Locale = Locale.ENGLISH_US,
) -> str:
    """
    Fetch the live patch notes HTML from Blizzard

    Raises:
        ParserBlizzardError: If Blizzard returns non-200 status
    """
    url = f"{settings.blizzard_host}/{locale}{settings.patch_notes_path}"
    response = await client.get(url, headers={"Accept": "text/html"})
    validate_response_status(response)
    return response.text


def _text_lines(node: LexborNode | None) -> list[str]:
    """Flatten a note body into one string per paragraph or bullet.

    Blizzard mixes ``<ul><li>`` bullets with the occasional ``<p><strong>``
    sub-heading inside the same block. Both are content, so both are kept, in
    document order — no ``<p>`` is ever nested inside an ``<li>`` on this page.
    """
    if node is None:
        return []

    lines = []
    for element in node.css("p, li"):
        # &nbsp; is common in Blizzard's prose and survives .text() as \xa0.
        text = " ".join(element.text().split())
        if text:
            lines.append(text)
    return lines


def _section_kind(section: LexborNode) -> str:
    """Return Blizzard's section kind, e.g. ``hero_update`` or ``map_update``."""
    classes = (section.attributes.get("class") or "").split()
    return next(
        (
            class_name.removeprefix(_SECTION_CLASS_PREFIX)
            for class_name in classes
            if class_name.startswith(_SECTION_CLASS_PREFIX)
        ),
        "",
    )


def _resolve_hero_key(name: str, hero_keys: Mapping[str, str] | None) -> str | None:
    """Resolve a patch-note hero name to its key.

    With a locale index, that index is the only source: the English heroes.csv
    is not consulted as a second chance, because a localised name may well be
    another hero's English name and a wrong mapping is worse than ``None``.
    """
    if hero_keys is None:
        return get_hero_key(name)

    return hero_keys.get(normalize_hero_name(name))


def _parse_hero_entry(
    hero_update: LexborNode, hero_keys: Mapping[str, str] | None
) -> dict:
    """Parse one ``.PatchNotesHeroUpdate`` block."""
    name = safe_get_text(hero_update.css_first(".PatchNotesHeroUpdate-name"))

    return {
        "title": name,
        # A hero Blizzard just shipped is not in any heroes list on patch day.
        # Keep the entry with its raw name rather than dropping it — a silently
        # missing hero is exactly the rot this repo keeps getting bitten by.
        "hero": _resolve_hero_key(name, hero_keys),
        "details": _text_lines(
            hero_update.css_first(".PatchNotesHeroUpdate-generalUpdates")
        ),
        "abilities": [
            {
                "name": safe_get_text(
                    ability.css_first(".PatchNotesAbilityUpdate-name")
                ),
                "details": _text_lines(
                    ability.css_first(".PatchNotesAbilityUpdate-detailList")
                ),
            }
            for ability in hero_update.css(".PatchNotesAbilityUpdate")
        ],
    }


def _parse_generic_entry(
    generic_update: LexborNode, _hero_keys: Mapping[str, str] | None
) -> dict:
    """Parse one ``.PatchNotesGeneralUpdate`` block."""
    return {
        "title": safe_get_text(
            generic_update.css_first(".PatchNotesGeneralUpdate-title")
        ),
        "hero": None,
        "details": _text_lines(
            generic_update.css_first(".PatchNotesGeneralUpdate-description")
        ),
        "abilities": [],
    }


def _parse_map_entry(
    map_update: LexborNode, _hero_keys: Mapping[str, str] | None
) -> dict:
    """Parse one ``.PatchNotesMapUpdate`` block.

    A map update is a named area plus a before/after image slider, and nothing
    else — there is no text to extract. ponytail: the screenshots are dropped;
    add them when a client actually wants to render the slider.
    """
    return {
        "title": safe_get_text(map_update.css_first(".PatchNotesMapUpdate-name")),
        "hero": None,
        "details": [],
        "abilities": [],
    }


# One selector for all three block kinds, so entries come back in document
# order whatever a section happens to contain.
_ENTRY_PARSERS = {
    "PatchNotesHeroUpdate": _parse_hero_entry,
    "PatchNotesGeneralUpdate": _parse_generic_entry,
    "PatchNotesMapUpdate": _parse_map_entry,
}
_ENTRY_SELECTOR = ", ".join(f".{class_name}" for class_name in _ENTRY_PARSERS)


def _parse_entry(entry: LexborNode, hero_keys: Mapping[str, str] | None) -> dict:
    """Dispatch one update block to the parser for its kind."""
    classes = (entry.attributes.get("class") or "").split()
    parser = next(
        _ENTRY_PARSERS[class_name]
        for class_name in classes
        if class_name in _ENTRY_PARSERS
    )
    return parser(entry, hero_keys)


def _parse_section(section: LexborNode, hero_keys: Mapping[str, str] | None) -> dict:
    """Parse one ``.PatchNotes-section`` block."""
    entries = [_parse_entry(entry, hero_keys) for entry in section.css(_ENTRY_SELECTOR)]

    return {
        "title": safe_get_text(section.css_first(".PatchNotes-sectionTitle")) or None,
        "kind": _section_kind(section),
        "description": "\n".join(
            _text_lines(section.css_first(".PatchNotes-sectionDescription"))
        )
        or None,
        # A map update repeats its area name only on the first of its sliders,
        # so the extra slots carry neither a title nor any text. Nothing to
        # serve, so they are not served.
        "entries": [entry for entry in entries if entry["title"] or entry["details"]],
    }


def _patch_date(patch: LexborNode) -> str:
    """Return the patch date as ``YYYY-MM-DD``, read from its anchor id.

    Raises:
        ParserParsingError: If the anchor is missing or not ``patch-<date>``
    """
    anchor = patch.css_first(".anchor")
    anchor_id = (anchor.attributes.get("id") or "") if anchor else ""

    date = anchor_id.removeprefix("patch-")
    if not date or date == anchor_id:
        msg = f"Patch anchor id is missing or unexpected: {anchor_id!r}"
        raise ParserParsingError(msg)

    return date


def parse_patch_notes_html(
    html: str, hero_keys: Mapping[str, str] | None = None
) -> list[dict]:
    """
    Parse the live patch notes page into structured data

    Args:
        html: Raw HTML content from the Blizzard patch notes page
        hero_keys: Normalised hero name → key index for the page's locale, as
            built by ``build_hero_key_index``. ``None`` (the default, and what
            ``en-us`` uses) matches against the English heroes.csv instead.

    Returns:
        List of patch dicts (newest first, the order Blizzard renders) with keys:
        ``date`` (ISO-8601 ``YYYY-MM-DD``), ``title`` and ``sections``.

    Raises:
        ParserParsingError: If parsing fails
    """
    try:
        root_tag = parse_html_root(html)

        return [
            {
                "date": _patch_date(patch),
                "title": safe_get_text(patch.css_first(".PatchNotes-patchTitle")),
                "sections": [
                    _parse_section(section, hero_keys)
                    for section in patch.css(".PatchNotes-section")
                ],
            }
            for patch in root_tag.css(".PatchNotes-patch")
        ]

    except (AttributeError, KeyError, IndexError, TypeError) as error:
        error_msg = f"Failed to parse patch notes HTML: {error!r}"
        raise ParserParsingError(error_msg) from error


def limit_patch_notes(patch_notes: list[dict], limit: int | None) -> list[dict]:
    """Keep only the *limit* most recent patches."""
    return patch_notes[:limit] if limit else patch_notes
