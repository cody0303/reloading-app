FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY static/ ./static/

# SQLite database lives on a named volume so data persists across container restarts
ENV DB_PATH=/data/reloading.db

RUN mkdir -p /data

EXPOSE 5000

# Use gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "app:app"]
