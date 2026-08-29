import pytest

from app.domain.parsers.heroes import parse_heroes_html
from app.domain.utils.helpers import build_hero_key_index
from tests.helpers import read_html_file


@pytest.fixture(scope="package")
def patch_notes_html_data():
    return read_html_file("patch-notes.html")


@pytest.fixture(scope="package")
def patch_notes_fr_html_data():
    return read_html_file("patch-notes-fr-fr.html")


@pytest.fixture(scope="package")
def heroes_fr_html_data():
    return read_html_file("heroes-fr-fr.html")


@pytest.fixture(scope="package")
def fr_hero_keys(heroes_fr_html_data: str) -> dict[str, str]:
    """The fr-fr name → key index, built exactly as the service builds it."""
    return build_hero_key_index(parse_heroes_html(heroes_fr_html_data))
