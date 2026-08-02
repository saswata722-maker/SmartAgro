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
# - 2 workers per CPU core is a common starting point for I/O-bound Flask apps
# - bind to 0.0.0.0 so the container port is reachable from outside
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "2", "--timeout", "120", "app:app"]