"""ARB-A — the ORM models and the migrations must not drift apart.

Two files describe the same five tables. Nothing makes them agree except
somebody remembering, and the failure is quiet: a column added to the model and
not to the SQL works perfectly on SQLite in the test suite and raises
`UndefinedColumn` the first time it meets a real database — which, for a
decision log, is in production and at the worst moment.

This walks the shipped PostgreSQL migrations and compares them to the mapped
columns, and does the same for the MySQL siblings, so a column added to one
dialect and forgotten in the other is caught too.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from velaris_shared.db.models import (
    ArbiterDecisionChainModel,
    ArbiterDecisionModel,
    ArbiterDraftModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
)

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
FILES = ("177_hxarbiter_policies", "178_hxarbiter_decisions",
         "179_hxarbiter_drafts", "180_arbiter_anchor_and_retention",
         "181_arbiter_chain_prune")

MODELS = (
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
    ArbiterDecisionModel,
    ArbiterDecisionChainModel,
    ArbiterDraftModel,
)

#: Anything that opens a line inside CREATE TABLE but is not a column.
_NOT_A_COLUMN = re.compile(
    r"^(PRIMARY|UNIQUE|CHECK|CONSTRAINT|FOREIGN|KEY|INDEX)\b", re.I
)
#: A column line: an identifier followed by whitespace and a type word.
_COLUMN = re.compile(r"^([a-z_][a-z0-9_]*)\s+[A-Za-z]")


def _read(dialect: str) -> str:
    return "".join(
        (MIGRATIONS / dialect / f"{f}.sql").read_text() for f in FILES
    )


def _read_without_comments(dialect: str) -> str:
    """The SQL alone. The comments in these files discuss `DEFAULT` and
    `GENERATED ALWAYS AS` at length, precisely because both are traps — so a
    check that scans the raw text finds the warning and calls it the offence.
    The first version of the two tests below did exactly that."""
    out = []
    for line in _read(dialect).splitlines():
        stripped = line.split("--", 1)[0]
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def _columns_in_sql(sql: str, table: str) -> set[str]:
    """Column names declared for `table`, tracking parenthesis depth.

    Depth matters: a multi-line CHECK constraint contains lines that look like
    identifiers, and a naive line scan reports them as columns. That is exactly
    what a first attempt at this test did.
    """
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", sql, re.S
    )
    assert m, f"{table} not found in the migration"

    cols: set[str] = set()
    depth = 0
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if depth == 0 and line and not line.startswith("--"):
            if not _NOT_A_COLUMN.match(line):
                hit = _COLUMN.match(line)
                if hit:
                    cols.add(hit.group(1))
        depth += raw.count("(") - raw.count(")")

    # Columns added by a later ALTER in the same migration set.
    # `ADD COLUMN IF NOT EXISTS a, ADD COLUMN IF NOT EXISTS b` in one statement
    # is legal and mig 181 uses it, so every occurrence is collected rather
    # than only the one that follows the table name.
    for stmt in re.findall(rf"ALTER TABLE {table}\s+(.*?);", sql, re.S):
        cols |= set(re.findall(
            r"ADD COLUMN IF NOT EXISTS ([a-z_][a-z0-9_]*)", stmt))
    # MySQL 8.4 and MariaDB 11.8 have no `ADD COLUMN IF NOT EXISTS`, so the
    # sibling migrations wrap the ALTER in an information_schema guard and a
    # PREPARE. Without this second pattern the MySQL half of this test would go
    # on passing while a column existed in one dialect and not the other —
    # which is the exact drift the file was written to catch.
    for stmt in re.findall(rf"'ALTER TABLE {table} (.*?)'", sql):
        cols |= set(re.findall(r"ADD COLUMN ([a-z_][a-z0-9_]*)", stmt))
    return cols


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
def test_postgres_migration_matches_the_model(model):
    sql = _read("postgresql")
    assert _columns_in_sql(sql, model.__tablename__) == {
        c.name for c in model.__table__.columns
    }


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__tablename__)
def test_mysql_sibling_matches_the_model(model):
    sql = _read("mysql")
    assert _columns_in_sql(sql, model.__tablename__) == {
        c.name for c in model.__table__.columns
    }


def test_the_column_parser_ignores_a_multi_line_check():
    """Guards the test itself — the first version of it reported the CHECK
    constraint's own lines as columns and called that drift."""
    sql = """CREATE TABLE IF NOT EXISTS t (
    a  UUID NOT NULL,
    b  VARCHAR(36) NOT NULL,
    CONSTRAINT ck CHECK (
        (a IS NULL     AND b = 'platform') OR
        (a IS NOT NULL AND b = a::text)
    )
);"""
    assert _columns_in_sql(sql, "t") == {"a", "b"}


def test_every_arbiter_table_ships_in_both_dialects():
    pg, my = _read("postgresql"), _read("mysql")
    for model in MODELS:
        assert model.__tablename__ in pg
        assert model.__tablename__ in my


def test_mysql_never_defaults_a_microsecond_timestamp():
    """MySQL 8.4 rejects `DEFAULT now()` on DATETIME(6) where MariaDB accepts
    it. Running only one of the two is how that reaches a customer."""
    for line in _read_without_comments("mysql").splitlines():
        if "DATETIME(6)" in line.upper():
            assert "DEFAULT" not in line.upper(), line.strip()


def test_no_generated_column_in_the_mysql_siblings():
    """MariaDB 11.8 refuses COALESCE inside GENERATED ALWAYS AS (error 1901)
    where MySQL 8.4 accepts it — it created three tables of five before this
    was found. Nothing here may reintroduce one."""
    sql = _read_without_comments("mysql").upper()
    assert "GENERATED ALWAYS AS" not in sql
    assert " AS (" not in sql
