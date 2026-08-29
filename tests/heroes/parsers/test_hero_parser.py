"""Tests for _parse_birthday_and_age in app/domain/parsers/hero.py"""

import pytest
from selectolax.lexbor import LexborHTMLParser

from app.domain.enums import Locale
from app.domain.parsers.hero import (
    _parse_birthday_and_age,
    parse_ability_description,
    parse_hero_html,
)


@pytest.mark.parametrize(
    ("text", "locale", "expected"),
    [
        # en-us / en-gb
        ("Unknown", Locale.ENGLISH_US, (None, None)),
        ("Aug 8 (Age: 22)", Locale.ENGLISH_US, ("Aug 8", 22)),
        ("Aug 28 (Age: 32)", Locale.ENGLISH_US, ("Aug 28", 32)),
        ("Jan 1 (Age: 62)", Locale.ENGLISH_US, ("Jan 1", 62)),
        ("Unknown (Age: 32)", Locale.ENGLISH_US, (None, 32)),
        ("May 9 (Age: 1)", Locale.ENGLISH_US, ("May 9", 1)),
        ("May 9 (Age: 1)", Locale.ENGLISH_EU, ("May 9", 1)),
        # de-de
        ("9. Mai (Alter: 1)", Locale.GERMAN, ("9. Mai", 1)),
        ("Unbekannt (Alter: 32)", Locale.GERMAN, (None, 32)),
        # fr-fr
        ("9 mai (âge : 1 an)", Locale.FRENCH, ("9 mai", 1)),
        ("Inconnu (âge : 32 ans)", Locale.FRENCH, (None, 32)),
        # it-it
        ("9 mag (Età: 1)", Locale.ITALIANO, ("9 mag", 1)),
        # es-es
        ("9 may (Edad: 1)", Locale.SPANISH_EU, ("9 may", 1)),
        # pl-pl
        ("9 maj (Wiek: 1)", Locale.POLISH, ("9 maj", 1)),
        # ja-jp
        ("5月9日 （年齢: 1）", Locale.JAPANESE, ("5月9日", 1)),
        # ru-ru
        ("9 мая (возраст: 1 г.)", Locale.RUSSIAN, ("9 мая", 1)),  # noqa: RUF001
        # ko-kr
        ("5월 9일 (나이: 1)", Locale.KOREAN, ("5월 9일", 1)),
        # zh-tw
        ("5月9日 （年齡：1）", Locale.CHINESE_TAIWAN, ("5月9日", 1)),
        # pt-br
        ("Desconhecido (Idade: 10)", Locale.PORTUGUESE_BRAZIL, (None, 10)),
    ],
)
def test_parse_birthday_and_age(
    text: str,
    locale: Locale,
    expected: tuple[str | None, int | None],
):
    result = _parse_birthday_and_age(text, locale)

    assert result == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Aug 8",  # date with no parenthetical age block
    ],
)
def test_parse_birthday_and_age_no_match_returns_none_none(text: str):
    result = _parse_birthday_and_age(text, Locale.ENGLISH_US)

    assert result == (None, None)


class TestAbilityFireModes:
    """Blizzard marks primary/secondary fire with a mouse-button <img> whose alt
    is the same untranslated i18n key in every locale — the distinction is
    conveyed purely visually. `.text()` dropped both the marker and left a
    double space where it stood.
    """

    _MARKED = (
        '<p slot="description">'
        "<img alt='overwatch.page.herodetail.ability.primary-fire'/>"
        " Long-range rifle that heals allies and damages enemies. "
        "<img alt='overwatch.page.herodetail.ability.secondary-fire'/>"
        " Hold to zoom in.</p>"
    )

    @staticmethod
    def _node(html: str):
        return LexborHTMLParser(html).css_first("p")

    def test_description_no_longer_carries_the_gap_left_by_the_image(self):
        description, _ = parse_ability_description(self._node(self._MARKED))

        assert "  " not in description
        assert description == (
            "Long-range rifle that heals allies and damages enemies. Hold to zoom in."
        )

    def test_each_marked_sentence_is_attributed_to_its_fire_mode(self):
        _, fire_modes = parse_ability_description(self._node(self._MARKED))

        assert fire_modes == [
            {
                "mode": "primary",
                "description": (
                    "Long-range rifle that heals allies and damages enemies."
                ),
            },
            {"mode": "secondary", "description": "Hold to zoom in."},
        ]

    def test_unmarked_ability_reports_no_fire_modes(self):
        """Most abilities carry no marker; they must not gain an empty entry."""
        node = self._node('<p slot="description">Roll and reload.</p>')

        description, fire_modes = parse_ability_description(node)

        assert description == "Roll and reload."
        assert fire_modes == []

    def test_missing_node_is_not_an_error(self):
        result = parse_ability_description(None)

        assert result == ("", [])

    @pytest.mark.parametrize("hero_html_data", ["ana", "genji"], indirect=True)
    def test_real_hero_pages_split_only_weapon_abilities(self, hero_html_data: str):
        hero = parse_hero_html(hero_html_data, Locale.ENGLISH_US)
        marked = [a for a in hero["abilities"] if a["fire_modes"]]

        assert all("  " not in a["description"] for a in hero["abilities"])
        assert all(
            fm["mode"] in ("primary", "secondary")
            for a in marked
            for fm in a["fire_modes"]
        )


class TestSubrolePassive:
    @pytest.mark.parametrize(
        "hero_html_data", ["ana", "reinhardt", "genji", "mercy"], indirect=True
    )
    def test_passive_is_read_from_the_tooltip(self, hero_html_data: str):
        """Blizzard publishes the subrole's passive nowhere else."""
        hero = parse_hero_html(hero_html_data, Locale.ENGLISH_US)

        assert hero["subrole_passive"]
        assert hero["subrole_passive"] == hero["subrole_passive"].strip()
