# dd-dse-db — "DSE + DB"  Domino compute environment
# cache-bust: 2026-06-13
#
# Extends the Domino Standard Environment (DSE) base with auto-connect tooling
# for database apps running in the same project. Use this environment for
# workspaces, apps, or endpoints that need direct access to Domino DB apps.
#
# Paste into Domino's "Dockerfile Instructions" field.
# Base image: Domino Standard Environment (Ubuntu 22.04 / Python 3.10+).
# Do NOT add a FROM line.
#
# What this adds on top of DSC:
#   /opt/dd/workspace-setup.py   — discovers running DB apps, starts tunnels
#   /opt/dd/domino-db-tunnel.py  — WebSocket→TCP relay (zero deps, stdlib only)
#   psql / mongosh / redis-cli / mysql — CLI clients
#
# preRunScript to set on the environment revision:
#
#     python3 /opt/dd/workspace-setup.py
#
# On start the preRunScript:
#   1. Calls /v4/modelProducts to find running DB apps in the project.
#   2. Starts a background tunnel for each (detached, survives script exit).
#   3. Writes db() shell helper + per-app aliases to ~/.bashrc.
#   4. Prints a connection table to the init log.
#
# User experience — nothing to do:
#   db pg-fin4              → psql -h 127.0.0.1 -p 5432 -U domino -d postgres
#   db mongo-analytics      → mongosh 127.0.0.1:27017
#   db                      → list all available databases

USER root

# DB client CLIs — each RUN is a single line (Domino's env builder does not
# support backslash-continuation across pipes).
RUN apt-get update && apt-get install -y --no-install-recommends gnupg curl ca-certificates lsb-release && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/pgdg.asc && echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 redis-tools default-mysql-client && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb.gpg && echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-7.0.list && apt-get update && apt-get install -y --no-install-recommends mongodb-mongosh && rm -rf /var/lib/apt/lists/*

# Install dd auto-connect scripts from the Database-Extension repo.
# Client auto-connect scripts baked as a FALLBACK. A workspace in THIS
# project can point its preRunScript at /mnt/code/client/dd-workspace-setup.py
# instead, so a failed fetch (private repo without a token) must NOT abort the
# build. Pass --build-arg GITHUB_TOKEN=<pat> for a private repo.
ARG GITHUB_TOKEN=
RUN mkdir -p /opt/dd && ( set -e; if [ -n "$GITHUB_TOKEN" ]; then curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; else curl -fsSL https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz; fi && tar -xzf /tmp/dd-src.tar.gz -C /tmp && cp /tmp/Database-Extension-main/client/domino-db-tunnel.py /opt/dd/domino-db-tunnel.py && cp /tmp/Database-Extension-main/client/dd-workspace-setup.py /opt/dd/workspace-setup.py && chmod +x /opt/dd/domino-db-tunnel.py /opt/dd/workspace-setup.py && rm -rf /tmp/dd-src.tar.gz /tmp/Database-Extension-main ) || echo "[dd] WARN: could not bake client scripts from GitHub at build time (private repo without GITHUB_TOKEN, or no network). In THIS project, point preRunScript at /mnt/code/client/dd-workspace-setup.py. For cross-project drop-in, rebuild with --build-arg GITHUB_TOKEN=<pat> or make the repo public."

RUN mkdir -p /var/log/dd && chown -R ubuntu:ubuntu /var/log/dd /opt/dd

USER ubuntu
