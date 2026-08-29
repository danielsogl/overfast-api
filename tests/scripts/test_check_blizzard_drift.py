"""Hitpoints appear nowhere on Blizzard's site, so heroes.csv is hand-maintained
and rots after balance patches. The *changes* are published though, and this is
the logic that reads them. Every sample below is real patch-note wording.
"""

import pytest
from scripts.check_blizzard_drift import hitpoint_findings, parse_rotation_maps

_ROWS = {
    "Reaper": {"health": "275", "armor": "0", "shields": "0"},
    "Sigma": {"health": "350", "armor": "0", "shields": "250"},
    "Junkrat": {"health": "200", "armor": "0", "shields": "0"},
    "Reinhardt": {"health": "400", "armor": "300", "shields": "0"},
}

# Verbatim from https://overwatch.blizzard.com/en-us/news/patch-notes/live/2026/
_REAPER = (
    "Reaper Dire Triggers has increased Reaper's strengths across many "
    "matchups. Reducing his health lowers his survivability while preserving "
    "the lethality Dire Triggers provides. Health reduced from 300 to 275."
)
_SIGMA = (
    "Sigma Sigma remains durable when successfully rotating through his "
    "defensive tools. Shield health reduced from 275 to 250."
)


def _levels(findings: list[tuple[str, str]]) -> list[str]:
    return [level for level, _ in findings]


class TestStaleValueIsAFailure:
    """The high-signal case: our value is still the pre-patch one."""

    def test_stale_health_fails(self):
        rows = {**_ROWS, "Reaper": {**_ROWS["Reaper"], "health": "300"}}

        findings = hitpoint_findings(_REAPER, rows)

        assert _levels(findings) == ["fail"]
        assert "Reaper health is 300" in findings[0][1]
        assert "changed it to 275" in findings[0][1]

    def test_stale_shields_fails(self):
        rows = {**_ROWS, "Sigma": {**_ROWS["Sigma"], "shields": "275"}}

        findings = hitpoint_findings(_SIGMA, rows)

        assert _levels(findings) == ["fail"]
        assert "Sigma shields is 275" in findings[0][1]


class TestCurrentValueIsSilent:
    """A run that reports nothing is the normal state; noise gets ignored."""

    @pytest.mark.parametrize("text", [_REAPER, _SIGMA])
    def test_up_to_date_value_produces_no_finding(self, text: str):
        findings = hitpoint_findings(text, _ROWS)

        assert findings == []


class TestImplausibleDeltasAreIgnored:
    def test_barrier_health_is_not_hero_health(self):
        """Reinhardt's barrier is described as "Health" too, but 1500 is not a
        number any hero row holds, so it must not produce noise."""
        text = (
            "Reinhardt Barrier Field is too forgiving at the current value. "
            "Health reduced from 1500 to 1100."
        )

        findings = hitpoint_findings(text, _ROWS)

        assert findings == []

    def test_ability_shields_are_not_hero_shields(self):
        text = "Sigma Kinetic Grasp Shields reduced from 25 to 15."

        findings = hitpoint_findings(text, _ROWS)

        assert findings == []


class TestAmbiguousDeltasWarnRatherThanFail:
    def test_value_matching_neither_side_warns(self):
        """Could be a misattributed ability, or a value that was already wrong
        before the patch — worth a look, not a red run."""
        text = "Junkrat Frag Launcher Health increased from 250 to 300."
        rows = {**_ROWS, "Junkrat": {**_ROWS["Junkrat"], "health": "200"}}

        findings = hitpoint_findings(text, rows)

        assert _levels(findings) == ["warn"]


class TestAttribution:
    def test_delta_is_attributed_to_the_nearest_preceding_hero(self):
        rows = {
            **_ROWS,
            "Reaper": {**_ROWS["Reaper"], "health": "300"},
            "Sigma": {**_ROWS["Sigma"], "shields": "275"},
        }

        findings = hitpoint_findings(f"{_SIGMA} {_REAPER}", rows)

        assert _levels(findings) == ["fail", "fail"]
        assert "Sigma" in findings[0][1]
        assert "Reaper" in findings[1][1]

    def test_no_known_hero_name_warns_instead_of_guessing(self):
        findings = hitpoint_findings("Health reduced from 300 to 275.", _ROWS)

        assert _levels(findings) == ["warn"]


class TestRotationMapParsing:
    """The rates page is the only structured map list Blizzard publishes, and it
    is served only when query parameters are present — the bare URL is a shell.
    """

    # Trimmed from the live page; the class attribute before `label` is real and
    # broke my first regex.
    _SELECT = (
        '<select class="blz-dropdown" data-label="map" id="filter-map-select">'
        '<option data-rqs="1,0" data-title="all_maps" value="all-maps">All Maps</option>'
        '<optgroup class="blz-subheading-text-lg" label="Control">'
        '<option data-rqs="1,0" data-title="Busan" value="busan">Busan</option>'
        '<option data-rqs="1,0" data-title="Ilios" value="ilios">Ilios</option>'
        "</optgroup>"
        '<optgroup class="blz-subheading-text-lg" label="Hybrid">'
        '<option data-rqs="1,0" data-title="King\'s Row" value="kings-row">'
        "King's Row</option>"
        "</optgroup></select>"
    )

    def test_maps_are_grouped_by_gamemode(self):
        result = parse_rotation_maps(self._SELECT)

        assert result["busan"] == ("Busan", "control")
        assert result["ilios"] == ("Ilios", "control")
        assert result["kings-row"] == ("King's Row", "hybrid")

    def test_the_all_maps_sentinel_is_not_a_map(self):
        """It sits outside every optgroup, so it must not be collected."""
        result = parse_rotation_maps(self._SELECT)

        assert "all-maps" not in result

    def test_shell_page_yields_nothing_rather_than_guessing(self):
        """Without query parameters Blizzard serves a page with no dropdown at
        all. That must warn, not report every map as missing."""
        result = parse_rotation_maps("<html><body>no dropdown here</body></html>")

        assert result == {}
