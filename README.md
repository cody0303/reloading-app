# Reloading Database — Setup & Deployment Guide

## Project Structure

```
reloading-app/
├── app.py               # Flask backend + REST API
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Orchestration (recommended)
├── .dockerignore
└── static/
    └── index.html       # Frontend (served by Flask)
```

---

## Option 1: Run Locally with Python

### Prerequisites
- Python 3.10+

### Steps

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the app (database auto-created as reloading.db in current directory)
python app.py
```

Open http://localhost:5000 in your browser.

To change the database path:
```bash
DB_PATH=/path/to/mydata.db python app.py
```

---

## Option 2: Docker (Recommended)

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac)
  or Docker Engine + Docker Compose (Linux)

### Quick Start with Docker Compose

```bash
# 1. Build and start the container
docker compose up -d

# 2. Open your browser
open http://localhost:5000    # Mac
start http://localhost:5000   # Windows
```

The `-d` flag runs it in the background. The database is stored in a named
Docker volume (`reloading-data`) and persists across container restarts and rebuilds.

### Common Commands

```bash
# View logs
docker compose logs -f

# Stop the app
docker compose down

# Stop and wipe ALL data (destructive!)
docker compose down -v

# Rebuild after code changes
docker compose up -d --build
```

---

## Option 3: Docker Without Compose

```bash
# Build the image
docker build -t reloading-app .

# Create a named volume for the database
docker volume create reloading-data

# Run the container
docker run -d \
  --name reloading-app \
  -p 5000:5000 \
  -v reloading-data:/data \
  --restart unless-stopped \
  reloading-app

# Open http://localhost:5000
```

---

## Hosting on a Server (LAN or VPS)

### Run on a home server / NAS

Same Docker steps as above. Access from other devices on your network using the
server's local IP address instead of `localhost`:

```
http://192.168.1.XX:5000
```

Find your server's IP with `ip addr show` (Linux) or `ipconfig` (Windows).

### Change the port

Edit `docker-compose.yml` and change `"5000:5000"` to e.g. `"8080:5000"` to
expose it on port 8080 on the host.

### Expose to the internet (optional)

If you want access outside your home network:

1. **Reverse proxy with nginx** — put nginx in front and handle HTTPS via Let's Encrypt.
2. **Cloudflare Tunnel** — zero-config, no port forwarding needed:
   ```bash
   cloudflared tunnel --url http://localhost:5000
   ```
3. **Tailscale** — secure private network between your devices; no public exposure.

> **Security note**: This app has no authentication. Do not expose it directly
> to the public internet without adding a login layer.

---

## Backup & Restore

### Backup the database

```bash
# Copy the SQLite file out of the volume
docker run --rm \
  -v reloading-data:/data \
  -v $(pwd):/backup \
  alpine cp /data/reloading.db /backup/reloading_backup.db
```

Or use the Export CSV button in the app for a portable text backup.

### Restore from backup

```bash
docker run --rm \
  -v reloading-data:/data \
  -v $(pwd):/backup \
  alpine cp /backup/reloading_backup.db /data/reloading.db
```

---

## REST API Reference

All endpoints return JSON.

| Method | Path                    | Description                    |
|--------|-------------------------|--------------------------------|
| GET    | /api/loads              | List/search records            |
| POST   | /api/loads              | Create a new record            |
| PUT    | /api/loads/{id}         | Update a record                |
| DELETE | /api/loads/{id}         | Delete a record                |
| GET    | /api/stats              | Total loads & cartridge count  |
| GET    | /api/distinct/{column}  | Unique values for autocomplete |
| GET    | /api/export             | Download all data as CSV       |
| POST   | /api/import             | Upload a CSV file              |

### Query parameters for GET /api/loads

| Param          | Description                          |
|----------------|--------------------------------------|
| search         | Full-text search across all fields   |
| cartridge      | Filter by cartridge (partial match)  |
| powder_type    | Filter by powder type                |
| bullet_weight  | Filter by bullet weight              |
| annealed       | Filter by annealed (Yes / No)        |
| sort           | Column to sort by (default: id)      |
| dir            | ASC or DESC (default: DESC)          |

### Example

```bash
curl "http://localhost:5000/api/loads?cartridge=308&sort=powder_weight&dir=ASC"
```
