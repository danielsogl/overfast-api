"""Both halves of the player parser must fail the same way.

``ParserParsingError`` reaches a registered handler that answers JSON and raises
an alert. Anything else escapes all four handlers as a plain-text 500 that
nothing reports — so an unwrapped parser is not just a crash, it is a silent
one. ``_parse_summary`` was wrapped and ``_parse_stats`` was not, which put the
markup-volatile half of the parser on the unmonitored side.

Both mutations below are real ``KeyError`` paths: they were confirmed to raise,
so these tests exercise the wrapper rather than passing vacuously.
"""

import pytest

from app.domain.exceptions import ParserParsingError
from app.domain.parsers.player_profile import parse_player_profile_html


class TestStatsParsingIsMonitored:
    @pytest.mark.parametrize(
        ("find", "replace", "missing"),
        [
            # A hero <option> carrying option-id but no value.
            ('<option value="', '<option data-value="', "value"),
            # A stats container whose class attribute is gone.
            ('<span class="stats-container', '<span data-x="stats-container', "class"),
        ],
    )
    @pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
    def test_broken_stats_markup_raises_the_monitored_error(
        self, player_html_data: str, find: str, replace: str, missing: str
    ):
        mutated = player_html_data.replace(find, replace, 1)

        with pytest.raises(ParserParsingError, match=missing):
            parse_player_profile_html(mutated)

    @pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
    def test_intact_profile_still_parses(self, player_html_data: str):
        result = parse_player_profile_html(player_html_data)

        assert result["summary"]["username"]
        assert result["stats"] is not None
