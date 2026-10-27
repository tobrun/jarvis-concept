#!/bin/sh
# Create the Jarvis index database and the pgvector extension, from config/db.env.
# Usage: scripts/db_setup.sh   (JARVIS_DB_URL in the environment overrides config/db.env)
# Assumes a running Postgres 17 with pgvector; it never starts a server.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
for pgbin in /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin /usr/lib/postgresql/17/bin; do
    if [ -d "$pgbin" ]; then
        PATH="$pgbin:$PATH"
        export PATH
        break
    fi
done

url=${JARVIS_DB_URL:-$(sed -n 's/^JARVIS_DB_URL=//p' "$root/config/db.env" | tail -n 1)}
if [ -z "$url" ]; then
    echo "db_setup: JARVIS_DB_URL is not set and config/db.env does not define it" >&2
    exit 1
fi

# postgresql://[user@host:port]/<dbname>[?params] -> <dbname>
rest=${url#*://}
case "$rest" in
    */*) rest=${rest#*/} ;;
    *) rest= ;;
esac
db=${rest%%\?*}
if [ -z "$db" ]; then
    echo "db_setup: no database name in $url" >&2
    exit 1
fi

if psql "$url" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    echo "db_setup: database $db exists"
else
    # The maintenance connection reuses the URL's host and port with the postgres database.
    maintenance=$(printf '%s' "$url" | sed -E "s#/$db([?]|\$)#/postgres\1#")
    createdb --maintenance-db="$maintenance" "$db"
    echo "db_setup: created database $db"
fi

psql "$url" -v ON_ERROR_STOP=1 -qc 'CREATE EXTENSION IF NOT EXISTS vector'
echo "db_setup: vector extension ready in $db"
