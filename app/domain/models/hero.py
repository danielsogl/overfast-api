"""TypedDicts for the heroes-list entry and single-hero detail shapes.

Both shapes are produced by ``app.domain.parsers.heroes`` /
``app.domain.parsers.hero`` and flow straight through ``HeroService`` into the
API layer's ``HeroShort`` / ``Hero`` response models (see
``app/api/models/heroes.py``) with no intermediate validation — a dropped or
renamed field would previously be invisible to ``ty`` because both sides were
plain ``dict``.

They are ``TypedDict`` rather than a dataclass or a Pydantic model on purpose:
parsed static data is persisted as JSONB (``static_data.parsed``) and read
back as a plain ``dict`` — see ``app/adapters/storage/postgres_storage.py``
and ``StaticDataService._get_parsed``. A ``TypedDict`` *is* a plain ``dict``
at runtime, so it round-trips through that column with zero behaviour
change. A dataclass or Pydantic model would not survive that round trip
unchanged and would break the caching layer.

StrEnum-through-JSONB: ``HeroGamemode`` and ``MediaType`` members are real
enum instances immediately after parsing, but they serialize to plain JSON
strings in the ``parsed`` JSONB column and come back as plain ``str`` on the
next cache hit — see ``StaticDataService._get_parsed``. Both a freshly parsed
value and a storage-read one reach the same consumers (``filter_heroes``,
the API response model), so the honest field type covers both: ``HeroGamemode
| str`` / ``MediaType | str``. ``HeroGamemode``/``MediaType`` alone would be a
lie about the storage-read case (a plain ``str`` is not an instance of a
``StrEnum`` subclass, even though the reverse holds and every consumer here
only relies on ``StrEnum``'s value-equality, never on ``isinstance``).
"""

from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from app.domain.enums import HeroGamemode, MediaType


class HeroListEntry(TypedDict):
    """One row of ``parse_heroes_html`` / ``filter_heroes`` output.

    Matches ``HeroShort`` field-for-field. ``role``/``subrole`` are the raw
    ``data-role``/``data-subrole`` HTML attribute values — the parser never
    casts them to the ``Role``/``SubRole`` enums, that coercion happens at the
    Pydantic response-model boundary.
    """

    key: str
    name: str
    portrait: str
    role: str
    subrole: str
    gamemodes: list[HeroGamemode | str]
    is_new: bool


class HeroBackground(TypedDict):
    url: str
    # Raw ``bp`` attribute tokens (e.g. "md", "lg"), never cast to
    # BackgroundImageSize in the parser.
    sizes: list[str]


class HitPoints(TypedDict):
    health: int
    armor: int
    shields: int
    total: int


class AbilityVideoLink(TypedDict):
    mp4: str
    webm: str


class AbilityVideo(TypedDict):
    thumbnail: str
    link: AbilityVideoLink


class AbilityFireMode(TypedDict):
    # The regex that produces this only ever captures one of these two
    # literals — see `_FIRE_MODE_IMG` in app/domain/parsers/hero.py.
    mode: str
    description: str


class Ability(TypedDict):
    name: str
    description: str
    fire_modes: list[AbilityFireMode]
    icon: str
    video: AbilityVideo | None


class Perk(TypedDict):
    name: str
    description: str
    icon: str


class PerksContainer(TypedDict):
    minor: list[Perk]
    major: list[Perk]


class StadiumPower(TypedDict):
    name: str
    description: str
    icon: str


class Media(TypedDict):
    # See module docstring: StrEnum-through-JSONB.
    type: MediaType | str
    link: str


class StoryChapter(TypedDict):
    title: str
    content: str
    picture: str


class Story(TypedDict):
    summary: str
    media: Media | None
    chapters: list[StoryChapter]


class HeroDetail(TypedDict):
    """``parse_hero_html`` merged with the heroes-list portrait and CSV
    hitpoints (see ``_merge_hero_data``). Matches ``Hero`` field-for-field.

    ``portrait``, ``hitpoints`` and ``stadium_powers`` are the only fields
    the parser/merge step can omit outright (as opposed to setting to
    ``None``) — see the ``try/except/else`` branches in ``_merge_hero_data``
    and the ``stadium_wrapper`` walrus in ``parse_hero_html``. Everything
    else is always present, even when its value is ``None``.
    """

    name: str
    description: str
    backgrounds: list[HeroBackground]
    portrait: NotRequired[str]
    role: str
    subrole: str
    subrole_passive: str | None
    location: str
    birthday: str | None
    age: int | None
    hitpoints: NotRequired[HitPoints]
    abilities: list[Ability]
    perks: PerksContainer | None
    story: Story | None
    stadium_powers: NotRequired[list[StadiumPower]]
