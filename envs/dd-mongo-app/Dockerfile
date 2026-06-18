# dd-mongo-app — Domino compute environment for a MongoDB "database app"
#
# Paste into Domino's "Dockerfile Instructions" field. Base image: Domino
# Standard Environment (Ubuntu 22.04 / Py 3.10). Do NOT add a FROM line.
#
# Process model when this image runs as a Domino App:
#   :8888  Flask router (dbapp/router.py) — only externally-bound port
#   :8978  mongo-express (Mongo admin UI, internal — fronted at /admin/)
#   :27017 mongod (internal only, single-node replSet rs0)
#
# Each RUN is one line — Domino's UI eats `\` continuations across pipes.
# USER root at the top — the DSE base image runs as a non-root user by
# default and apt-get / npm -g installs would fail without this.

USER root

# MongoDB 7.0 server + tools (mongodump / mongorestore / mongosh).
RUN apt-get update && apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates curl wget cron sudo && curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor && echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-7.0.list && apt-get update && apt-get install -y --no-install-recommends mongodb-org mongodb-database-tools && rm -rf /var/lib/apt/lists/*

# Node 20 LTS — needed for mongo-express. NodeSource keyring is the
# recommended apt route for Ubuntu 22.04. NOTE: the DSE base image
# ships with Node 12 (libnode-dev / libnode72) preinstalled, which
# conflicts on /usr/include/node/common.gypi when NodeSource's
# nodejs 20.x tries to unpack. Purge the old packages first.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg && DEBIAN_FRONTEND=noninteractive apt-get purge -y libnode-dev libnode72 nodejs npm || true && mkdir -p /etc/apt/keyrings && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

# mongo-express — MIT-licensed open-source MongoDB admin UI.
# Flags:
#   --unsafe-perm  npm running as root needs this so postinstall scripts
#                  don't EACCES on /root
#   --no-audit / --no-fund  cut noise; also reduces network calls so the
#                  install doesn't fail on a missing audit endpoint
#   --omit=optional  skip native-build optional deps (bcrypt etc.) that
#                  need g++/python3-dev which we don't ship
# On failure, dump the npm debug log so we don't have to chase a "log
# file you can't read" pointer (which is what 1.0.2 did silently).
# Pinned to 1.1.0-rc-4 — confirmed to be the latest available on the
# registry and supports the Mongo 7.0 wire protocol.
RUN npm install -g --unsafe-perm --no-audit --no-fund --omit=optional mongo-express@1.1.0-rc-4 || (echo '=== NPM DEBUG LOG ===' >&2 && cat /root/.npm/_logs/*-debug-*.log >&2 && false)

# Python deps for Flask router + snapshotter.
RUN pip install --no-cache-dir 'flask>=3.0' 'flask-sock>=0.7' 'flask-cors>=4.0' 'gunicorn>=21.2' 'requests>=2.31' 'websockets==12.0' 'httpx==0.27.0'

# Bake in dbapp/ and snapshotter/ so this env works in ANY Domino project,
# not just Database-Extension. Same shape as dd-postgres-app.
ARG GITHUB_TOKEN=
RUN mkdir -p /opt/dd && ( set -e; if [ -n "$GITHUB_TOKEN" ]; then curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; else curl -fsSL https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; fi && tar -xzf /tmp/dd-src.tar.gz -C /tmp && cp -r /tmp/Database-Extension-main/dbapp /opt/dd/dbapp && cp -r /tmp/Database-Extension-main/snapshotter /opt/dd/snapshotter && cp /tmp/Database-Extension-main/dbapp/app.sh /opt/dd/app.sh && chmod +x /opt/dd/app.sh && rm -rf /tmp/dd-src.tar.gz /tmp/Database-Extension-main ) || echo "[dd] WARN: could not bake dbapp/ from GitHub at build time (private repo without GITHUB_TOKEN, or no network). Image is still valid: DB Apps in THIS project run from /mnt/code/dbapp. For cross-project drop-in, rebuild with --build-arg GITHUB_TOKEN=<pat> or make the repo public."

RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd /opt/dd

ENV DD_ROLE=mongo DD_ENGINE=mongo DD_OPT_DIR=/opt/dd

USER ubuntu
