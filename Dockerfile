FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Chicago \
    CONFIG_DIR=/config \
    HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    HEB_MCP_COMMAND=/opt/heb/bin/texas-grocery-mcp \
    DISPLAY=:99

# HEB tool in its own venv (pinned), plus Chromium for session refresh and a virtual display,
# because texas-grocery-mcp logs in with a visible (headed) browser.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb \
 && python -m venv /opt/heb \
 && /opt/heb/bin/pip install "texas-grocery-mcp[browser]==0.1.3" \
 && /opt/heb/bin/playwright install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# Unraid convention: nobody:users (99:100) owns appdata.
RUN mkdir -p /config && chown 99:100 /config
USER 99:100
VOLUME ["/config"]

ENTRYPOINT ["docker-entrypoint.sh"]
