FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir uv==0.7.19
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY docs ./docs
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --create-home --uid 10001 researcher && mkdir -p /srv/.research && chown -R researcher:researcher /srv/.research
USER researcher
ENV PATH="/srv/.venv/bin:$PATH"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
