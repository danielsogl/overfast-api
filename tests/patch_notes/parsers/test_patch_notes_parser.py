"""Tests for the patch notes parser, against the real Blizzard page.

``tests/fixtures/html/patch-notes.html`` is the unmodified
``https://overwatch.blizzard.com/en-us/news/patch-notes/live`` page.
``patch-notes-fr-fr.html`` and ``heroes-fr-fr.html`` are the same pages in
``fr-fr``, fetched the same way.
"""

from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import status

from app.domain.enums import HeroKey, Locale
from app.domain.exceptions import ParserBlizzardError, ParserParsingError
from app.domain.parsers.patch_notes import (
    fetch_patch_notes_html,
    limit_patch_notes,
    parse_patch_notes_html,
)


class TestParsePatchNotesHtml:
    def test_patches_are_parsed_newest_first(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        dates = [patch["date"] for patch in patch_notes]

        assert dates
        assert dates == sorted(dates, reverse=True)

    def test_every_patch_has_a_title_and_an_iso_date(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        assert all(patch["title"] for patch in patch_notes)
        # fromisoformat raises on anything that is not a plain ISO-8601 date.
        assert [date.fromisoformat(patch["date"]) for patch in patch_notes]

    def test_sections_expose_blizzard_own_kinds(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        kinds = {
            section["kind"] for patch in patch_notes for section in patch["sections"]
        }

        assert kinds == {"generic_update", "hero_update", "map_update"}

    def test_section_description_is_captured(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        descriptions = [
            section["description"]
            for patch in patch_notes
            for section in patch["sections"]
            if section["description"]
        ]

        assert descriptions
        assert all(description.strip() for description in descriptions)

    def test_hero_updates_resolve_to_hero_keys(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        hero_entries = [
            entry
            for patch in patch_notes
            for section in patch["sections"]
            for entry in section["entries"]
            if section["kind"] == "hero_update"
        ]

        assert hero_entries
        assert all(entry["hero"] in HeroKey for entry in hero_entries)
        assert all(entry["title"] for entry in hero_entries)

    def test_unknown_hero_is_kept_with_a_null_key(self, patch_notes_html_data: str):
        # A hero Blizzard ships before heroes.csv knows it: same real markup,
        # a name that cannot resolve.
        html = patch_notes_html_data.replace(">D.Mon<", ">Brand New Hero<")

        patch_notes = parse_patch_notes_html(html)

        unknown = [
            entry
            for patch in patch_notes
            for section in patch["sections"]
            for entry in section["entries"]
            if entry["title"] == "Brand New Hero"
        ]
        assert unknown
        assert all(entry["hero"] is None for entry in unknown)
        assert all(entry["details"] or entry["abilities"] for entry in unknown)

    def test_hero_abilities_carry_their_own_details(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        abilities = [
            ability
            for patch in patch_notes
            for section in patch["sections"]
            for entry in section["entries"]
            for ability in entry["abilities"]
        ]

        assert abilities
        assert all(ability["name"] for ability in abilities)
        assert all(ability["details"] for ability in abilities)

    def test_map_entries_are_named_areas_without_text(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        map_entries = [
            entry
            for patch in patch_notes
            for section in patch["sections"]
            for entry in section["entries"]
            if section["kind"] == "map_update"
        ]

        assert map_entries
        assert all(entry["title"] for entry in map_entries)
        assert all(entry["details"] == [] for entry in map_entries)

    def test_no_entry_is_kept_without_any_content(self, patch_notes_html_data: str):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        empty = [
            entry
            for patch in patch_notes
            for section in patch["sections"]
            for entry in section["entries"]
            if not entry["title"] and not entry["details"]
        ]

        assert empty == []

    def test_text_only_announcement_has_one_untitled_section(
        self, patch_notes_html_data: str
    ):
        patch_notes = parse_patch_notes_html(patch_notes_html_data)

        untitled = [
            section
            for patch in patch_notes
            for section in patch["sections"]
            if section["title"] is None
        ]

        assert untitled
        assert all(section["description"] for section in untitled)

    def test_raises_on_missing_main_content(self):
        with pytest.raises(ParserParsingError):
            parse_patch_notes_html("<html><body>nope</body></html>")

    def test_raises_when_patch_anchor_is_unusable(self, patch_notes_html_data: str):
        html = patch_notes_html_data.replace('id="patch-2026-08-20"', 'id="nope"')

        with pytest.raises(ParserParsingError):
            parse_patch_notes_html(html)


def _hero_entries(patch_notes: list[dict]) -> list[dict]:
    return [
        entry
        for patch in patch_notes
        for section in patch["sections"]
        for entry in section["entries"]
        if section["kind"] == "hero_update"
    ]


class TestParseLocalisedPatchNotesHtml:
    def test_localised_names_do_not_resolve_against_the_english_csv(
        self, patch_notes_fr_html_data: str
    ):
        patch_notes = parse_patch_notes_html(patch_notes_fr_html_data)

        unresolved = {
            entry["title"] for entry in _hero_entries(patch_notes) if not entry["hero"]
        }

        assert {"Écho", "Chacal", "Danger", "Vital"} <= unresolved

    def test_localised_names_resolve_with_the_locale_index(
        self, patch_notes_fr_html_data: str, fr_hero_keys: dict[str, str]
    ):
        patch_notes = parse_patch_notes_html(patch_notes_fr_html_data, fr_hero_keys)

        hero_entries = _hero_entries(patch_notes)
        resolved = {entry["title"]: entry["hero"] for entry in hero_entries}

        assert hero_entries
        assert all(entry["hero"] in HeroKey for entry in hero_entries)
        assert resolved["Écho"] == HeroKey.ECHO
        assert resolved["Chacal"] == HeroKey.JUNKRAT
        assert resolved["Danger"] == HeroKey.HAZARD
        assert resolved["Vital"] == HeroKey.LIFEWEAVER

    def test_unknown_localised_name_is_kept_with_a_null_key(
        self, patch_notes_fr_html_data: str, fr_hero_keys: dict[str, str]
    ):
        # A hero shipped on patch day: it is named in the notes before it
        # appears in any locale's heroes list.
        html = patch_notes_fr_html_data.replace(">Écho<", ">Nouveau Héros<")

        patch_notes = parse_patch_notes_html(html, fr_hero_keys)

        unknown = [
            entry
            for entry in _hero_entries(patch_notes)
            if entry["title"] == "Nouveau Héros"
        ]
        assert unknown
        assert all(entry["hero"] is None for entry in unknown)
        assert all(entry["details"] or entry["abilities"] for entry in unknown)

    def test_english_page_is_unchanged_by_the_default_index(
        self, patch_notes_html_data: str
    ):
        without_index = parse_patch_notes_html(patch_notes_html_data)

        with_explicit_none = parse_patch_notes_html(patch_notes_html_data, None)

        assert with_explicit_none == without_index
        assert all(entry["hero"] in HeroKey for entry in _hero_entries(without_index))


class TestLimitPatchNotes:
    @pytest.mark.parametrize(
        ("limit", "expected"),
        [(None, 3), (0, 3), (1, 1), (2, 2), (99, 3)],
    )
    def test_limit_keeps_the_most_recent_patches(
        self, limit: int | None, expected: int
    ):
        patch_notes = [{"date": "2026-08-20"}, {"date": "2026-08-19"}, {"date": "x"}]

        result = limit_patch_notes(patch_notes, limit)

        assert len(result) == expected
        assert result == patch_notes[:expected]


class TestFetchPatchNotesHtml:
    @pytest.mark.asyncio
    async def test_returns_response_text(self, patch_notes_html_data: str):
        client = AsyncMock()
        client.get.return_value = Mock(
            status_code=status.HTTP_200_OK, text=patch_notes_html_data
        )

        result = await fetch_patch_notes_html(client, Locale.ENGLISH_US)

        assert result == patch_notes_html_data
        assert client.get.call_args[0][0].endswith("/en-us/news/patch-notes/live")

    @pytest.mark.asyncio
    async def test_locale_is_part_of_the_url(self):
        client = AsyncMock()
        client.get.return_value = Mock(status_code=status.HTTP_200_OK, text="")

        await fetch_patch_notes_html(client, Locale.FRENCH)

        assert "/fr-fr/news/patch-notes/live" in client.get.call_args[0][0]

    @pytest.mark.asyncio
    async def test_raises_on_blizzard_error(self):
        client = AsyncMock()
        client.get.return_value = Mock(
            status_code=status.HTTP_404_NOT_FOUND, text="Not Found"
        )

        with pytest.raises(ParserBlizzardError):
            await fetch_patch_notes_html(client, Locale.ENGLISH_US)
