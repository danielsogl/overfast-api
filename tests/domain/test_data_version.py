from typing import TYPE_CHECKING

from app.domain import parsers
from app.domain.parsers import DATA_VERSION, PARSER_VERSION, data_version

if TYPE_CHECKING:
    from pathlib import Path

_SMALLINT_MAX = 32767


def _csv_dir(tmp_path: Path, heroes: str) -> Path:
    (tmp_path / "heroes.csv").write_text(heroes)
    (tmp_path / "maps.csv").write_text("key,uses\naatlis,standard\n")
    return tmp_path


def test_a_csv_edit_changes_the_stamp(tmp_path: Path):
    """Stored rows parsed with the old hitpoints must stop matching."""
    before = data_version(2, _csv_dir(tmp_path, "key,armor\nmauga,125\n"))
    after = data_version(2, _csv_dir(tmp_path, "key,armor\nmauga,150\n"))

    assert before != after


def test_a_parser_bump_changes_the_stamp(tmp_path: Path):
    csv_dir = _csv_dir(tmp_path, "key,armor\nmauga,150\n")

    assert data_version(2, csv_dir) != data_version(3, csv_dir)


def test_the_stamp_is_stable_and_fits_the_column():
    assert DATA_VERSION >= 1
    assert DATA_VERSION <= _SMALLINT_MAX
    assert data_version(PARSER_VERSION, parsers._CSV_DIR) == DATA_VERSION
