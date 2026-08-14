# ── SmartAgro Docker Image ───────────────────────────────────────────────────
# Optimised for Hugging Face Spaces (port 7860) and any other container host.

FROM python:3.11-slim

# Keeps Python from generating .pyc files and ensures stdout/stderr is unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install dependencies first (layer-cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Hugging Face Spaces requires port 7860
EXPOSE 7860

# Run with Gunicorn for production reliability
# - A SINGLE worker is intentional: Shared in-memory state (rate limiters,
#   translation caches) must stay consistent; multiple workers each carry
#   their own copy. Concurrency comes from request *threads* instead, which
#   suits this I/O-bound app (network calls dominate). Move to Redis + more
#   workers if you ever need to scale beyond one process.
# - bind to 0.0.0.0 so the container port is reachable from outside
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]