#!/bin/sh
# Update the bot on Hostmaster. From the server/ folder (on Windows — in Git Bash):
#   sh deploy/deploy.sh          copy the code, rebuild and restart this bot's containers
#   sh deploy/deploy.sh build    copy the code and only build the image (nothing is started)
# .env, secrets/ and the database live on the server and are never touched by this script.
#
# The repository is public, so the server address is not kept here: put it into
# deploy/.deploy.local (ignored by git) as DEPLOY_HOST=user@address, or pass it in the environment.
set -eu

cd "$(dirname "$0")/.."
if [ -f deploy/.deploy.local ]; then
  . deploy/.deploy.local
fi
HOST="${DEPLOY_HOST:?set DEPLOY_HOST=user@address in deploy/.deploy.local (deploy/README.md)}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/hostmaster_bench}"
DIR="${DEPLOY_DIR:-proactive-school}"         # relative to the home folder on the server
DOCKER="${DEPLOY_DOCKER:-sudo -n docker}"      # the server user is not in the docker group
MODE="${1:-up}"

if [ "$MODE" = build ]; then
  RUN="$DOCKER build -q -t proactive-school-school-bot ."
else
  RUN="test -f .env || { echo 'No .env on the server: the owner puts it there (deploy/README.md)'; exit 1; }
      test -f secrets/google-service-account.json || echo 'Warning: no secrets/google-service-account.json, Google Sheets features stay off'
      $DOCKER compose up -d --build
      $DOCKER compose ps"
fi

# The files for the build are listed one by one, so .env, secrets/, tests and .git never leave this machine.
# app/ and migrations/ are replaced as a whole: a file deleted here must not stay on the server.
tar czf - --exclude=__pycache__ --exclude='*.pyc' \
    app migrations alembic.ini Dockerfile .dockerignore docker-compose.yml pyproject.toml uv.lock \
  | ssh -i "$KEY" -o BatchMode=yes "$HOST" "set -e
      mkdir -p $DIR/secrets; cd $DIR
      rm -rf app migrations
      tar xzf - --no-same-owner
      $RUN"
