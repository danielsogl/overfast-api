"""Domain parser modules for Blizzard data extraction"""

import zlib
from pathlib import Path

# Stamp written next to every parsed payload in storage. A stored row whose
# ``data_version`` differs from this value is re-parsed once on read and written
# back — that is what keeps a parser change effective immediately without
# putting a parse on every cache miss forever.
#
# Bump this whenever a parser's OUTPUT changes shape or content. Forgetting to
# bump means stored rows keep serving the old output until their own staleness
# threshold expires, which is the same failure the pre-parse design had.
#
# ponytail: one global int, so a change to any parser re-parses everything once.
# Split it into a per-category mapping only if that single re-parse pass ever
# shows up as a real cost — at this data volume it is a handful of parses.
PARSER_VERSION = 2

# What actually goes into ``data_version``: PARSER_VERSION plus a fingerprint of
# the CSVs that parsers merge into their output (hitpoints, map uses, ...).
# Editing heroes.csv used to change nothing here, so stored rows kept serving
# the old values for up to their 24h staleness threshold, and a row written by
# the old image during a rolling deploy was fresh and wrong for a whole day.
#
# ponytail: crc32 squeezed into data_version's SMALLINT range, 1 in 32767
# chance that a change keeps the old stamp. That costs no more than today's
# behaviour (stale until the threshold), and keeps the column and its tables
# as they are: widening it would rewrite player_profiles under an exclusive
# lock while the old app still serves. Widen the column if collisions matter.
_CSV_DIR = Path(__file__).resolve().parent.parent / "utils" / "data"


def data_version(parser_version: int, csv_dir: Path) -> int:
    """Stamp for stored parsed rows, in 1..32767 (the column is SMALLINT)."""
    csv_bytes = b"".join(path.read_bytes() for path in sorted(csv_dir.glob("*.csv")))
    return zlib.crc32(str(parser_version).encode() + csv_bytes) % 32767 + 1


DATA_VERSION = data_version(PARSER_VERSION, _CSV_DIR)
