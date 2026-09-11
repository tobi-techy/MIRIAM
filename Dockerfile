FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps (curl for healthcheck, build tools for deps with wheels missing)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY miriam_agent ./miriam_agent

RUN pip install --upgrade pip \
    && pip install --no-cache-dir "uvicorn[standard]" .

EXPOSE 8000

CMD ["uvicorn", "miriam_agent.cli:app", "--host", "0.0.0.0", "--port", "8000"]