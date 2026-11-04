FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Chicago \
    CONFIG_DIR=/config \
    HOME=/tmp

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# The entrypoint starts as root, fixes /config ownership, then drops to PUID:PGID
# (default 99:100, Unraid's nobody:users).
ENV PUID=99 PGID=100
RUN mkdir -p /config
VOLUME ["/config"]

ENTRYPOINT ["docker-entrypoint.sh"]
