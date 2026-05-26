# dd-mongo-app — Domino compute environment for a MongoDB "database app"
# Same shape as dd-postgres-app — only the engine differs. (No pgweb yet —
# pgweb is Postgres-only; Mongo admin UI lands in v1.)

USER root

RUN apt-get update && apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates curl wget cron sudo && curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor && echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" > /etc/apt/sources.list.d/mongodb-org-7.0.list && apt-get update && apt-get install -y --no-install-recommends mongodb-org mongodb-database-tools && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir 'flask>=3.0' 'flask-sock>=0.7' 'flask-cors>=4.0' 'gunicorn>=21.2' 'requests>=2.31' 'websockets==12.0' 'httpx==0.27.0'

RUN mkdir -p /mnt/db /var/log/dd && chown -R ubuntu:ubuntu /mnt/db /var/log/dd

ENV DD_ROLE=mongo DD_ENGINE=mongo

USER ubuntu
