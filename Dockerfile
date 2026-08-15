FROM node:20-alpine AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend /src/research_agent/static/react ./src/research_agent/static/react
RUN python -m pip install --upgrade pip && python -m pip install ".[mcp,openai,postgres,observability,distributed]"

EXPOSE 8000
CMD ["uvicorn", "research_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
