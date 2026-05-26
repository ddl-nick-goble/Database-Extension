# dd-postgres-env — Domino compute environment for a Postgres "database workspace"
#
# Paste into Domino's "Dockerfile Instructions" field. Base image: Domino
# Standard Environment (Ubuntu 22.04 / Py 3.10 — DSE already has JupyterLab).
# Do NOT add a FROM line — Domino prepends it.
#
# Architecture:
#   :8888  JupyterLab (the IDE — has jupyter-server-proxy, fronts everything)
#   :8978  CloudBeaver (background service, reached via /proxy/8978/)
#   :8765  ws2tcp     (background service, reached via /proxy/8765/wire)
#   :5432  Postgres   (internal only)
#
# All non-binary logic (preRun.sh, ws2tcp, snapshotter) lives at /mnt/code
# and is invoked by stub scripts in the image — so you iterate without
# rebuilding the env.

USER root

# -- System deps + Postgres 16 ---------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg lsb-release ca-certificates curl wget cron sudo \
        openjdk-17-jre-headless \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
        https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-16 postgresql-client-16 postgresql-contrib-16 \
    && rm -rf /var/lib/apt/lists/*

# -- jupyter-server-proxy is what makes /proxy/<port>/ work for sidecars.
#    Without it, neither CloudBeaver (:8978) nor ws2tcp (:8765) are reachable
#    from outside the pod.
RUN pip install --no-cache-dir \
        jupyter-server-proxy==4.5.0 \
        websockets==12.0 \
        httpx==0.27.0

# -- CloudBeaver -----------------------------------------------------------
ARG CLOUDBEAVER_VERSION=24.3.0
RUN mkdir -p /opt/cloudbeaver \
    && curl -fsSL "https://dbeaver.io/files/cloudbeaver-ce/${CLOUDBEAVER_VERSION}/cloudbeaver-ce-${CLOUDBEAVER_VERSION}-linux.x86_64.tar.gz" \
        | tar -xz -C /opt --strip-components=1 -f -

# -- Stub: preRun delegates to /mnt/code so we can iterate on it -----------
RUN mkdir -p /var/opt/domino && \
    printf '%s\n' '#!/bin/bash' \
        'set -e' \
        '# Project files at /mnt/code mount BEFORE preRun runs.' \
        'exec bash /mnt/code/envs/dd-postgres-env/preRun.sh' \
        > /var/opt/domino/preRun.sh && \
    chmod +x /var/opt/domino/preRun.sh

# -- Layout ---------------------------------------------------------------
RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd

ENV DD_ENGINE=postgres \
    DD_PG_DATA=/mnt/db/pgdata \
    DD_PG_PORT=5432 \
    DD_CLOUDBEAVER_PORT=8978 \
    DD_WS_PORT=8765 \
    DD_SNAPSHOT_INTERVAL_MIN=60

USER ubuntu
