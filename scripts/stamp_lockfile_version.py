"""Copy the project version from pyproject.toml into uv.lock.

semantic-release stamps ``pyproject.toml`` but knows nothing about ``uv.lock``,
which carries the project's own version in its ``[[package]]`` entry.  The two
then drift, and the next ``uv`` command anyone runs silently rewrites the lock —
producing an unrelated modified file in whatever branch they happen to be on.

Run as semantic-release's ``build_command``, after the version has been stamped
and before the release commit is made.  Nothing else in the lock is touched:
dependency pins and hashes are uv's business, and ``uv`` is not available in the
semantic-release action container anyway.

**This must never fail the release.**  A failing ``build_command`` aborts the
release, and the release is what deploys to production — far too high a price
for a cosmetic metadata field.  So every unexpected condition exits 0 with a
note, leaving the lock exactly as it was.
"""

import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    pyproject = ROOT / "pyproject.toml"
    lockfile = ROOT / "uv.lock"

    if not lockfile.is_file():
        print("uv.lock not found — nothing to stamp.")
        return 0

    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]

    text = lockfile.read_text(encoding="utf-8")
    # Anchored on the package's own entry so no dependency's version can match.
    pattern = re.compile(
        rf'(\[\[package\]\]\nname = "{re.escape(name)}"\nversion = ")[^"]*(")'
    )
    stamped, count = pattern.subn(rf"\g<1>{version}\g<2>", text)

    if count != 1:
        print(
            f"Expected exactly one {name!r} package entry in uv.lock, found "
            f"{count} — leaving the lock untouched.",
            file=sys.stderr,
        )
        return 0

    if stamped == text:
        print(f"uv.lock already at {version}.")
        return 0

    lockfile.write_text(stamped, encoding="utf-8")
    print(f"Stamped uv.lock to {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
