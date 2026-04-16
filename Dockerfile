# Single-stage build — straightforward and reliable.
FROM python:3.11-slim

# System deps: LibreOffice Impress (headless PPTX→PDF) + fonts
# libreoffice-java-common is intentionally omitted — not needed for conversion.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer, rebuilt only when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and frontend
COPY app/ .
COPY static/ ./static/

# LibreOffice needs a writable home for its user-profile on first launch.
# Running as root (default) with HOME=/tmp keeps things simple for a local tool.
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    DISPLAY=

EXPOSE 8080

# Railway injects $PORT; fall back to 8080 for local Docker use.
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
