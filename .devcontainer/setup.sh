#!/usr/bin/env bash
# Runs once when the Codespace is created.
set -euo pipefail

echo ">>> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq postgresql postgresql-contrib

echo ">>> Starting Postgres"
sudo service postgresql start

echo ">>> Creating local dev database"
sudo -u postgres psql <<SQL
CREATE USER vigil WITH PASSWORD 'vigil';
CREATE DATABASE vigil OWNER vigil;
GRANT ALL PRIVILEGES ON DATABASE vigil TO vigil;
SQL

echo ">>> Installing Python dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo ">>> Creating .env from template (if missing)"
if [ ! -f .env ]; then
  cp .env.example .env
  SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" .env
  echo "    .env created with a fresh SECRET_KEY"
fi

echo ">>> Running database migrations"
flask --app app db upgrade || echo "(migrations will run on first dev launch)"

cat <<'EOF'

==============================================================
  Vigil Codespace ready.

  Run the dev server:
    flask --app app --debug run --host 0.0.0.0 --port 8080

  Or with auto-reload:
    flask --app app --debug run --host 0.0.0.0 --port 8080 --reload

  Create your first admin user:
    flask --app app create-admin

  Useful commands:
    flask --app app db migrate -m "describe change"   # create migration
    flask --app app db upgrade                        # apply migrations
    flask --app app db downgrade                      # roll back

==============================================================
EOF
