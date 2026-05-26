# dd-mongo-env — Domino compute environment for a MongoDB "database workspace"
#
# Same shape as dd-postgres-env: JupyterLab is the IDE on :8888 (with
# jupyter-server-proxy), CloudBeaver and ws2tcp run as sidecars reached via
# /proxy/<port>/. Paste into Domino "Dockerfile Instructions" — no FROM.

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg lsb-release ca-certificates curl wget cron sudo \
        openjdk-17-jre-headless \
    && curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor \
    && echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        mongodb-org mongodb-database-tools \
    && rm -rf /var/lib/apt/lists/*

# jupyter-server-proxy gates the /proxy/<port>/ routing
RUN pip install --no-cache-dir \
        jupyter-server-proxy==4.5.0 \
        websockets==12.0 \
        httpx==0.27.0

ARG CLOUDBEAVER_VERSION=24.3.0
RUN mkdir -p /opt/cloudbeaver \
    && curl -fsSL "https://dbeaver.io/files/cloudbeaver-ce/${CLOUDBEAVER_VERSION}/cloudbeaver-ce-${CLOUDBEAVER_VERSION}-linux.x86_64.tar.gz" \
        | tar -xz -C /opt --strip-components=1 -f -

# preRun stub — real logic at /mnt/code
RUN mkdir -p /var/opt/domino && \
    printf '%s\n' '#!/bin/bash' \
        'set -e' \
        'exec bash /mnt/code/envs/dd-mongo-env/preRun.sh' \
        > /var/opt/domino/preRun.sh && \
    chmod +x /var/opt/domino/preRun.sh

RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd

ENV DD_ENGINE=mongo \
    DD_MONGO_DATA=/mnt/db/mongo \
    DD_MONGO_PORT=27017 \
    DD_CLOUDBEAVER_PORT=8978 \
    DD_WS_PORT=8765 \
    DD_SNAPSHOT_INTERVAL_MIN=60

USER ubuntu
