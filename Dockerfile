FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[local-research]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin drbenchpro \
    && mkdir -p /data/runs \
    && chown -R drbenchpro:drbenchpro /app /data

USER drbenchpro

ENV DRBP_RUN_DIR=/data/runs \
    DRBP_WIKI_BASE_URL=http://wiki-search:8091 \
    DRBP_HOST=0.0.0.0 \
    DRBP_PORT=8090

EXPOSE 8090

CMD ["drbp-research-serve"]
