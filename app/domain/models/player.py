"""Player domain model dataclasses and TypedDicts.

The TypedDicts below describe the stable, named parts of parser output that
``PlayerService`` and the API layer both depend on. ``TypedDict`` was picked
deliberately over a dataclass or a Pydantic model: a ``TypedDict`` *is* a
plain ``dict`` at runtime, so ``parse_player_profile_html``'s return value
round-trips through the ``player_profiles.parsed`` JSONB column (see
``app/adapters/storage/postgres_storage.py`` and
``PlayerService._parse_stored``) with zero runtime change. A dataclass or a
Pydantic model would not survive that JSONB round-trip as the same type.
"""

from dataclasses import dataclass, field
from typing import Any, TypedDict


class BlizzardSearchPlayer(TypedDict, total=False):
    """One entry from Blizzard's account-search endpoint.

    This is the raw shape ``parse_player_summary_json`` hands back (and what
    ``PlayerIdentity.player_summary`` carries) — *not* the API's
    ``PlayerSummary`` model. Every field is optional: a no-match search
    returns ``{}`` (see ``parse_player_summary_json``), and Blizzard payloads
    vary by region — some carry ``portrait`` instead of
    ``avatar``/``namecard``/``title``. The profile parser only ever reads a
    handful of these keys, always through ``.get()``.
    """

    name: str
    isPublic: bool
    lastUpdated: int
    avatar: str | None
    namecard: str | None
    title: str | None
    portrait: str | None
    url: str


class PlayerEndorsementData(TypedDict):
    """``PlayerSummary.endorsement`` shape — see ``_get_endorsement``.

    Both fields are always set together: ``_get_endorsement`` returns either
    ``None`` or a dict with both keys, never a partial one.
    """

    level: int
    frame: str


class CompetitiveRankData(TypedDict):
    """One role's rank — the ``PlayerCompetitiveRank`` shape.

    ``_get_platform_competitive_ranks`` only ever builds this dict with all
    five fields set; a role it can't fully read (unknown division/tier) is
    skipped entirely rather than reported with some fields missing.
    """

    division: str
    tier: int
    role_icon: str
    rank_icon: str
    tier_icon: str


class PlatformCompetitiveRanksData(TypedDict):
    """Per-platform ranks — the ``PlatformCompetitiveRanksContainer`` shape.

    ``_get_platform_competitive_ranks`` always sets ``season`` and all four
    roles (``None`` for an unranked or unreadable one), so nothing here is
    conditionally omitted.
    """

    season: int | None
    tank: CompetitiveRankData | None
    damage: CompetitiveRankData | None
    support: CompetitiveRankData | None
    open: CompetitiveRankData | None


class CompetitiveRanksData(TypedDict):
    """``PlayerSummary.competitive`` shape.

    ``_get_competitive_ranks`` always sets both platform keys (``None`` when
    a platform has no data at all).
    """

    pc: PlatformCompetitiveRanksData | None
    console: PlatformCompetitiveRanksData | None


class PlayerProfileSummary(TypedDict):
    """``_parse_summary``'s return — maps onto ``PlayerSummary`` in
    ``app/api/models/players.py``.

    All seven keys are always present (values themselves can be ``None``);
    ``_parse_summary`` builds this as one dict literal, nothing is
    conditionally omitted, so ``total=False`` would only hide a real parser
    regression from ``ty``.
    """

    username: str
    avatar: str | None
    namecard: str | None
    title: str | None
    endorsement: PlayerEndorsementData | None
    competitive: CompetitiveRanksData | None
    last_updated_at: int | None


class PlayerProfileData(TypedDict):
    """``parse_player_profile_html``'s return — maps onto ``Player`` in
    ``app/api/models/players.py``.

    ``stats`` is deliberately left as a loose dict, not a TypedDict: it nests
    platform -> gamemode -> hero key -> stat, and every one of those keys is
    dynamic (driven by whatever heroes/platforms Blizzard exposes for that
    player). A TypedDict there would describe a shape that does not exist.
    """

    summary: PlayerProfileSummary
    stats: dict[str, Any] | None


@dataclass
class PlayerIdentity:
    """Result of player identity resolution.

    Groups the four fields that travel together after resolving a
    BattleTag or Blizzard ID to a canonical identity.
    """

    blizzard_id: str | None = field(default=None)
    player_summary: BlizzardSearchPlayer = field(default_factory=BlizzardSearchPlayer)
    cached_html: str | None = field(default=None)
    battletag_input: str | None = field(default=None)
