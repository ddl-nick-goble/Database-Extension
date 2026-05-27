# dd-redis-app — Domino compute environment for a Redis "database app"
#
# Paste into Domino's "Dockerfile Instructions" field. Base image: Domino
# Standard Environment (Ubuntu 22.04 / Py 3.10). Do NOT add a FROM line.
#
# Process model when this image runs as a Domino App:
#   :8888  Flask router (dbapp/router.py) — only externally-bound port
#   :8978  redis-commander (admin UI, internal — fronted at /admin/)
#   :6379  redis-server (internal only, AOF + RDB persistence)
#
# Each RUN is one line — Domino's UI eats `\` continuations across pipes.

USER root

# Redis 7.x server + tools (redis-cli).
RUN apt-get update && apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates curl wget cron sudo redis-server redis-tools && rm -rf /var/lib/apt/lists/*

# Node 20 LTS for redis-commander. The DSE base ships Node 12 via
# libnode-dev / libnode72 which conflict on /usr/include/node/common.gypi
# when the NodeSource nodejs 20.x .deb tries to unpack. Purge first.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg && DEBIAN_FRONTEND=noninteractive apt-get purge -y libnode-dev libnode72 nodejs npm || true && mkdir -p /etc/apt/keyrings && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

# redis-commander — MIT-licensed OSS Redis admin UI. Pinned to a tested
# release. `npm install -g` puts the binary at /usr/bin/redis-commander.
RUN npm install -g redis-commander@0.8.0

# Python deps for Flask router + snapshotter.
RUN pip install --no-cache-dir 'flask>=3.0' 'flask-sock>=0.7' 'flask-cors>=4.0' 'gunicorn>=21.2' 'requests>=2.31' 'websockets==12.0' 'httpx==0.27.0'

# Bake dbapp/ + snapshotter/ from this repo. Pulled from main.
RUN mkdir -p /opt/dd && curl -fsSL https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main -o /tmp/dd-src.tar.gz && tar -xzf /tmp/dd-src.tar.gz -C /tmp && cp -r /tmp/Database-Extension-main/dbapp /opt/dd/dbapp && cp -r /tmp/Database-Extension-main/snapshotter /opt/dd/snapshotter && cp /tmp/Database-Extension-main/dbapp/app.sh /opt/dd/app.sh && chmod +x /opt/dd/app.sh && rm -rf /tmp/dd-src.tar.gz /tmp/Database-Extension-main

RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd /opt/dd

ENV DD_ROLE=redis DD_ENGINE=redis DD_OPT_DIR=/opt/dd

USER ubuntu
