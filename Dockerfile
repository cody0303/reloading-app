FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY sm_decode.py .
COPY static/ ./static/

# SQLite database lives on a named volume so data persists across container restarts
ENV DB_PATH=/data/reloading.db

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

RUN mkdir -p /data

EXPOSE 5000

# Docker (and Portainer) mark the container unhealthy when the app stops answering. /api/stats runs a few
# database queries, so it also catches a database that can't be read. The slim image has no curl, hence Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/stats', timeout=4)"

# Use gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "app:app"]
