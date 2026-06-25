# ── n8n MCP Backup Server ─────────────────────────────────────────────────────
#
# Multi-stage build is not needed here (no compiled assets), but we pin the
# base image to a specific minor version for reproducibility.
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Keeps Python from buffering stdout/stderr so logs appear in `docker logs`
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install postgresql-client for pg_dump / pg_restore / psql
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching – rebuilt only when
# requirements.txt changes, not on every code edit)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# backups/ is created at runtime via the bind-mount in docker-compose.yml.
# Creating it here ensures it exists even if run standalone (e.g. in tests).
RUN mkdir -p /app/backups

# Default: keep the container alive so Claude Desktop can exec into it.
# Override with `python server.py` to run the MCP server directly.
CMD ["tail", "-f", "/dev/null"]
