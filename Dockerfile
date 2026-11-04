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

# Healthy = the bot reached Telegram in the last 3 minutes and no other copy is polling the same token.
# Unraid shows this as healthy / unhealthy on the Docker tab.
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD ["python", "-m", "family_assistant", "healthcheck"]

ENTRYPOINT ["docker-entrypoint.sh"]
