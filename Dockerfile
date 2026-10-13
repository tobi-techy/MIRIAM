FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS base
# Supply-chain: the base is pinned by digest (the multi-arch index digest of
# python:3.11-slim). Refresh deliberately with:
#   docker pull python:3.11-slim \
#     && docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# System deps: curl for healthcheck, build-essential only for pip build then purged
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# The image builds from uv.lock, not from the loose pyproject ranges: CI
# checks lock freshness that deployment used to ignore, so what is tested is
# not what shipped. --frozen refuses to run if the lock is stale.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY miriam_agent ./miriam_agent
# Alembic and the DB bootstrap scripts ship inside the image so a deploy can
# run `alembic upgrade head` from the container itself (the image is
# self-sufficient: no checkout, no host tooling). psycopg2-binary is a runtime
# dependency for that sync path — see alembic/env.py and pyproject.toml.
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
    && uv sync --frozen --no-dev \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv sync installs into /app/.venv; put it on PATH for entrypoint and probes.
ENV PATH="/app/.venv/bin:$PATH"

# Least privilege: non-root user
RUN useradd -m -u 10001 miriam && chown -R miriam:miriam /app
USER miriam

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
