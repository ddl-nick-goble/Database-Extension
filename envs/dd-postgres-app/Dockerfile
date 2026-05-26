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
# Each RUN is a single line — Domino's UI eats `\` continuations across
# pipes. No probe commands (--version/--help). USER root is required at
# the top — the DSE base image runs as a non-root user by default and
# apt-get / chown will fail without this.

USER root

RUN apt-get update && apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates curl wget cron sudo unzip && install -d /usr/share/postgresql-common/pgdg && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && apt-get update && apt-get install -y --no-install-recommends postgresql-16 postgresql-client-16 postgresql-contrib-16 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/pgweb.zip https://github.com/sosedoff/pgweb/releases/download/v0.17.0/pgweb_linux_amd64.zip && unzip /tmp/pgweb.zip -d /tmp && mv /tmp/pgweb_linux_amd64 /usr/local/bin/pgweb && chmod +x /usr/local/bin/pgweb && rm /tmp/pgweb.zip

RUN pip install --no-cache-dir 'flask>=3.0' 'flask-sock>=0.7' 'flask-cors>=4.0' 'gunicorn>=21.2' 'requests>=2.31' 'websockets==12.0' 'httpx==0.27.0'

RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd

ENV DD_ROLE=postgres DD_ENGINE=postgres

USER ubuntu
