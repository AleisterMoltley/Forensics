FROM python:3.11-slim

# Install curl for Railway healthcheck + build deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create data dir for local dev fallback
RUN mkdir -p data

# Railway sets PORT dynamically
ENV PORT=8080
EXPOSE ${PORT}

# Healthcheck — Railway uses this to verify the service is up
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Use exec form so SIGTERM reaches Python directly (important for Railway deploys)
CMD ["python", "-u", "-m", "src.main"]
