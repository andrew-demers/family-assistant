FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Chicago \
    CONFIG_DIR=/config

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Unraid convention: nobody:users (99:100) owns appdata.
RUN mkdir -p /config && chown 99:100 /config
USER 99:100
VOLUME ["/config"]

ENTRYPOINT ["python", "-m", "family_assistant"]
