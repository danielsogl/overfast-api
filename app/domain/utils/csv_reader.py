"""CSV reader for domain static data files"""

import csv
from functools import cache
from pathlib import Path


@cache
def read_csv_file(filename: str) -> list[dict[str, str]]:
    """Read a CSV file by name (without extension) from the data/ directory.

    Cached: these files ship with the image and only change on deploy, which
    restarts the process. Without this, /maps and /gamemodes re-read and
    re-parse their file from disk on every request that misses the API cache,
    and every hero request re-reads heroes.csv for its hitpoints.

    Callers must treat the result as read-only — it is shared between them.
    All current ones build new structures by comprehension.
    """
    csv_path = Path(__file__).parent / "data" / f"{filename}.csv"
    with csv_path.open(encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file, delimiter=","))
