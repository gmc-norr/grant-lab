#!/usr/bin/env bash
# Cutover script for B2 (call-monitor → call-manager) deployment.
#
# Renames the Postgres database + role from call_monitor to call_manager.
# MUST be run from a session NOT connected to either database (use psql -d postgres).
# MUST be run after the app process is stopped and before the new app process starts
# with DATABASE_URL pointing at call_manager.
#
# Idempotent: re-running after a successful cutover is a no-op (the IF EXISTS guards
# turn the second run into nothing).
#
# Required env vars:
#   PGHOST, PGPORT, PGUSER (a superuser, NOT call_monitor — that role is being renamed),
#   PGPASSWORD.

set -euo pipefail

if [[ -z "${PGUSER:-}" ]]; then
    echo "PGUSER must be set to a superuser (NOT call_monitor)" >&2
    exit 1
fi

echo "Connecting as PGUSER=$PGUSER to PGHOST=${PGHOST:-localhost}…"

# Step 1: rename the database. Requires no active connections to call_monitor.
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'call_monitor') THEN
        ALTER DATABASE call_monitor RENAME TO call_manager;
        RAISE NOTICE 'Renamed database call_monitor → call_manager';
    ELSE
        RAISE NOTICE 'call_monitor database does not exist (already renamed?); skipping';
    END IF;
END
\$\$;
SQL

# Step 2: rename the role.
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'call_monitor') THEN
        ALTER ROLE call_monitor RENAME TO call_manager;
        RAISE NOTICE 'Renamed role call_monitor → call_manager';
    ELSE
        RAISE NOTICE 'call_monitor role does not exist; skipping';
    END IF;
END
\$\$;
SQL

# Step 3: Postgres invalidates the password on RENAME; restore it.
# (The password matches the dev-env default; production deployments
# rotate this via the env-var pattern documented in .env.example.)
psql -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE call_manager WITH PASSWORD 'call_manager';"

echo "Cutover complete. Bring up the new app with DATABASE_URL pointing at call_manager."
