# Universal Agent Platform — application image.
#
# Two stages: the builder resolves every runtime dependency from pyproject.toml
# into a venv (the setuptools build backend never reaches the final image); the
# runtime stage is slim + venv + the repo tree the server runs from. PostgreSQL
# with pgvector is a separate container — see compose.yaml.

FROM python:3.13-slim AS builder

# The package is installed editable because uap.server.app locates the UI as
# <repo>/ui and expects migrations at ./migrations — i.e. the app is designed
# to run from the repo tree (the same layout as the documented `pip install -e`
# dev install). One copy of the source, served in place.
ENV PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e .

FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=postgresql+psycopg://uap:uap_local_dev@db:5432/uap

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

# Runtime files only — no build tooling, no tests, no docs.
COPY src ./src
COPY ui ./ui
COPY migrations ./migrations
COPY alembic.ini ./

# Non-root. /app/data/runs is the events/artifacts root (JsonlSink, PlatformSlice);
# it is a VOLUME so runs survive container replacement.
RUN useradd --uid 10001 --create-home uap \
 && mkdir -p /app/data/runs \
 && chown -R uap:uap /app/data

EXPOSE 8000
VOLUME ["/app/data/runs"]
USER uap

# "/" serves the UI shell and is exempt from UAP_API_TOKEN, so the probe answers
# with or without auth. Plain python — the slim base ships no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"

# Apply migrations (retrying while PostgreSQL comes up — compose gates the app
# on `db` being healthy, explicit runs get the same safety net), then serve.
CMD set -e; \
    tries=0; \
    until alembic upgrade head; do \
      tries=$((tries + 1)); \
      if [ "$tries" -ge 15 ]; then \
        echo "uap: PostgreSQL not reachable at $DATABASE_URL after $tries attempts" >&2; \
        exit 1; \
      fi; \
      echo "uap: waiting for PostgreSQL ($tries/15)..."; \
      sleep 2; \
    done; \
    exec uvicorn uap.server.app:create_app --factory --host 0.0.0.0 --port 8000
