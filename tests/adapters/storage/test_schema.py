"""Tests for app/adapters/storage/schema.sql.

The schema is executed in full on every boot (``PostgresStorage._create_schema``),
against a database that usually already has everything in it. Every statement
must therefore be a no-op the second time, and the enum in particular has a
trap: its ``DO $$ ... IF NOT EXISTS`` block only runs when the type is ABSENT,
so on the live database a value added to :class:`StaticDataCategory` would never
reach postgres and every write in that category would fail. The ``ALTER TYPE
... ADD VALUE IF NOT EXISTS`` lines are what closes that gap; these tests fail
if a member ever loses one.
"""

import re

import pytest

from app.adapters.storage.postgres_storage import _SCHEMA_SQL
from app.domain.ports.storage import StaticDataCategory

_CREATE_TYPE_VALUES = re.compile(
    r"CREATE TYPE static_data_category AS ENUM \(([^)]*)\)"
)


class TestSchemaEnumCoverage:
    def test_create_type_lists_every_category(self):
        match = _CREATE_TYPE_VALUES.search(_SCHEMA_SQL)
        assert match is not None

        declared = set(re.findall(r"'([^']+)'", match.group(1)))

        assert declared == {category.value for category in StaticDataCategory}

    @pytest.mark.parametrize("category", list(StaticDataCategory))
    def test_every_category_has_an_idempotent_alter(self, category: StaticDataCategory):
        statement = (
            "ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS "
            f"'{category.value}';"
        )

        occurrences = _SCHEMA_SQL.count(statement)

        assert occurrences == 1


class TestSchemaIdempotency:
    def test_every_alter_type_is_guarded(self):
        alters = re.findall(r"^ALTER TYPE .*$", _SCHEMA_SQL, flags=re.MULTILINE)

        assert alters
        assert all("ADD VALUE IF NOT EXISTS" in alter for alter in alters)

    @pytest.mark.parametrize("keyword", ["CREATE TABLE", "CREATE INDEX"])
    def test_tables_and_indexes_use_if_not_exists(self, keyword: str):
        statements = re.findall(rf"^\s*{keyword}\b.*$", _SCHEMA_SQL, flags=re.MULTILINE)

        assert statements
        assert all("IF NOT EXISTS" in statement for statement in statements)

    def test_create_type_only_runs_when_the_type_is_absent(self):
        assert "IF NOT EXISTS (SELECT 1 FROM pg_type" in _SCHEMA_SQL
