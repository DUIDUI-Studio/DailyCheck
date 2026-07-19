FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install OS deps (none critical for now; keep slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8080

# Dev mode: Flask dev server (foreground, port 8080) + MCP HTTP server (background, port 5100).
# A trivial shell supervisor avoids a multi-process init system.
CMD ["sh", "-c", "python -m mcp_server --transport http --host 0.0.0.0 --port 5100 > /tmp/mcp.log 2>&1 & exec flask --app app run --host 0.0.0.0 --port 8080 --debug"]
