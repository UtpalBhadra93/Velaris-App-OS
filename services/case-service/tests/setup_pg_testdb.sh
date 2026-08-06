#!/usr/bin/env bash
# Set up the opt-in Postgres test database for the case-service suite.
# The conftest builds the schema itself (DROP SCHEMA public CASCADE + create_all +
# velaris_case_seq), so this only needs to create an EMPTY velaris_test database.
#
# Usage:
#   bash tests/setup_pg_testdb.sh
#   export VELARIS_TEST_DATABASE_URL="postgresql+asyncpg://velaris:<PW>@localhost:5432/velaris_test"
#   pytest                       # now runs against Postgres (REQUIRES_PG tests included)
#
# Get <PW> from the container:  docker exec docker-compose-velaris-db-1 env | grep POSTGRES_PASSWORD
# SERIAL ONLY: the schema is built once per run via a module-global flag — do NOT
# use pytest-xdist (-n); parallel workers would concurrently DROP SCHEMA the same DB.
set -euo pipefail
C="${DB_CONTAINER:-docker-compose-velaris-db-1}"
docker exec "$C" psql -U velaris -d postgres -q \
  -c "DROP DATABASE IF EXISTS velaris_test;" \
  -c "CREATE DATABASE velaris_test OWNER velaris;"
echo "velaris_test created. Set VELARIS_TEST_DATABASE_URL and run pytest."
