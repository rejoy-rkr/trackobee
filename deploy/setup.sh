#!/bin/sh
# One-time setup on the laptop: python venv, dependencies, .env
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env; chmod 600 .env
  KEY=$(.venv/bin/python -c "import secrets;print(secrets.token_hex(32))")
  sed -i "s/^TB_SECRET_KEY=.*/TB_SECRET_KEY=$KEY/" .env
  echo "Created .env - edit DATABASE_URL and TB_PUBLIC_URL, then run:"
else
  echo ".env already exists. Next:"
fi
cat <<'TXT'
  .venv/bin/python manage.py check                      # tests the Supabase connection and creates the tables
  .venv/bin/python manage.py create-owner you@email     # your platform-owner login
  sh deploy/install-services.sh                          # runs Trackobee + the tunnel in the background, auto-start
TXT
