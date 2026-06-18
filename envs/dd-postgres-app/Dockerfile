# dd-postgres-app — Domino compute environment for a Postgres "database app"
#
# Paste into Domino's "Dockerfile Instructions" field. Base image: Domino
# Standard Environment (Ubuntu 22.04 / Py 3.10). Do NOT add a FROM line.
#
# Process model when this image runs as a Domino App:
#   :8888  Flask router (dbapp/router.py) — only externally-bound port
#   :8978  pgweb (Postgres admin UI, internal — fronted at /admin/)
#   :5432  Postgres (internal only)
#
# What lives in the image (so dd-postgres-app works in ANY Domino project,
# not just Database-Extension):
#   /opt/dd/dbapp/         — lifecycle.py + router.py + dispatcher app.sh
#   /opt/dd/snapshotter/   — snapshot_postgres.py
#   /usr/local/bin/pgweb   — Go binary
#   postgres-16            — apt
#
# The Domino App's entry script is still `./app.sh` in the project's
# /mnt/code (Domino's convention). For DB-hosting projects, that file is a
# one-line shim: `exec /opt/dd/app.sh`. Any project can copy that single
# line and start hosting databases — no other repo dependency.
#
# Each RUN is a single line — Domino's UI eats `\` continuations across
# pipes. No probe commands (--version/--help). USER root is required at
# the top — the DSE base image runs as a non-root user by default and
# apt-get / chown will fail without this.

USER root

RUN apt-get update && apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates curl wget cron sudo unzip pgbouncer && install -d /usr/share/postgresql-common/pgdg && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && apt-get update && apt-get install -y --no-install-recommends postgresql-16 postgresql-client-16 postgresql-contrib-16 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/pgweb.zip https://github.com/sosedoff/pgweb/releases/download/v0.17.0/pgweb_linux_amd64.zip && unzip /tmp/pgweb.zip -d /tmp && mv /tmp/pgweb_linux_amd64 /usr/local/bin/pgweb && chmod +x /usr/local/bin/pgweb && rm /tmp/pgweb.zip

RUN pip install --no-cache-dir 'flask>=3.0' 'flask-sock>=0.7' 'flask-cors>=4.0' 'gunicorn>=21.2' 'requests>=2.31' 'websockets==12.0' 'httpx==0.27.0'

# dbapp/ + snapshotter/ are baked from GitHub as a FALLBACK only. At runtime
# the DB App prefers THIS project's /mnt/code/dbapp (see dispatcher app.sh),
# so a failed fetch here (private repo without a token, or no network) must
# NOT abort the image build. Pass --build-arg GITHUB_TOKEN=<pat> for a private repo.
ARG GITHUB_TOKEN=
RUN mkdir -p /opt/dd && ( set -e; if [ -n "$GITHUB_TOKEN" ]; then curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; else curl -fsSL https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; fi && tar -xzf /tmp/dd-src.tar.gz -C /tmp && cp -r /tmp/Database-Extension-main/dbapp /opt/dd/dbapp && cp -r /tmp/Database-Extension-main/snapshotter /opt/dd/snapshotter && cp /tmp/Database-Extension-main/dbapp/app.sh /opt/dd/app.sh && chmod +x /opt/dd/app.sh && rm -rf /tmp/dd-src.tar.gz /tmp/Database-Extension-main ) || echo "[dd] WARN: could not bake dbapp/ from GitHub at build time (private repo without GITHUB_TOKEN, or no network). Image is still valid: DB Apps in THIS project run from /mnt/code/dbapp. For cross-project drop-in, rebuild with --build-arg GITHUB_TOKEN=<pat> or make the repo public."

RUN mkdir -p /mnt/db /mnt/db/sock /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd /opt/dd

ENV DD_ROLE=postgres DD_ENGINE=postgres DD_OPT_DIR=/opt/dd

USER ubuntu
