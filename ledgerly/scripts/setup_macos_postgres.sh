#!/usr/bin/env bash
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh"
  exit 1
fi

if ! brew list --versions postgresql@18 >/dev/null 2>&1; then
  brew install postgresql@18
fi

brew services start postgresql@18
postgres_prefix="$(brew --prefix postgresql@18)"
export PATH="$postgres_prefix/bin:$PATH"

for _attempt in {1..20}; do
  if pg_isready -q; then
    break
  fi
  sleep 1
done
if ! pg_isready -q; then
  echo "PostgreSQL did not become ready. Run: brew services list"
  exit 1
fi

if [[ -z "${OLX_DB_PASSWORD:-}" ]]; then
  read -r -s -p "Password for olx_bot: " OLX_DB_PASSWORD
  echo
fi
if [[ -z "${OLX_READER_PASSWORD:-}" ]]; then
  read -r -s -p "Password for olx_reader: " OLX_READER_PASSWORD
  echo
fi
export OLX_DB_PASSWORD OLX_READER_PASSWORD

psql postgres \
  --set=ON_ERROR_STOP=1 \
  --set=bot_password="$OLX_DB_PASSWORD" \
  --set=reader_password="$OLX_READER_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE olx_bot LOGIN PASSWORD %L', :'bot_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'olx_bot') \gexec
SELECT format('ALTER ROLE olx_bot PASSWORD %L', :'bot_password') \gexec

SELECT format('CREATE ROLE olx_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'olx_reader') \gexec
SELECT format('ALTER ROLE olx_reader PASSWORD %L', :'reader_password') \gexec

SELECT 'CREATE DATABASE olx_business OWNER olx_bot'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'olx_business') \gexec

GRANT CONNECT ON DATABASE olx_business TO olx_bot, olx_reader;
SQL

psql olx_business --set=ON_ERROR_STOP=1 <<'SQL'
CREATE SCHEMA IF NOT EXISTS business AUTHORIZATION olx_bot;
CREATE SCHEMA IF NOT EXISTS bot AUTHORIZATION olx_bot;
CREATE SCHEMA IF NOT EXISTS analytics AUTHORIZATION olx_bot;

GRANT USAGE ON SCHEMA business, analytics TO olx_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA business, analytics TO olx_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE olx_bot IN SCHEMA business
    GRANT SELECT ON TABLES TO olx_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE olx_bot IN SCHEMA analytics
    GRANT SELECT ON TABLES TO olx_reader;

ALTER ROLE olx_bot IN DATABASE olx_business
    SET search_path TO business, analytics, bot, public;
ALTER ROLE olx_reader IN DATABASE olx_business
    SET search_path TO analytics, business, public;
SQL

encoded_password="$(python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["OLX_DB_PASSWORD"], safe=""))')"
echo
echo "PostgreSQL is ready. Put this in .env:"
echo "DATABASE_URL=postgresql://olx_bot:${encoded_password}@127.0.0.1:5432/olx_business"

