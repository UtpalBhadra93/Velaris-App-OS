FROM python:3.12-slim AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY libs/ /app/libs/
COPY engine/ /app/engine/
# velaris-engine depends on velaris-shared, a workspace member: uv.lock
# names it as an editable path, so `uv sync --frozen` cannot build an
# installation plan without the directory actually being here.
COPY services/_shared/ /app/services/_shared/
COPY pyproject.toml uv.lock /app/
COPY velaris.yaml /app/
# Database driver, as above. The engine declares no mysql extra and adding
# one would invalidate uv.lock, which --frozen rejects, so the drivers go
# in beside the synced environment instead.
ARG DB_EXTRAS=""
RUN uv sync --frozen --no-dev --package velaris-engine \
 && if [ -n "$DB_EXTRAS" ]; then uv pip install aiomysql pymysql; fi
EXPOSE 8100
CMD ["uv", "run", "uvicorn", "helix_engine.main:app", "--host", "0.0.0.0", "--port", "8100"]
