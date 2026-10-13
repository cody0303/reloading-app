# Reloading Log

A self-hosted reloading log: load recipes, component inventory, firearms, and range performance, with
ShotMarker target data and Garmin Xero chronograph files linked to each session. Flask + SQLite on the
back, a single-page vanilla JS front end, no build step.

- **Loads** – recipes with lot numbers, searchable and sortable; prints a 50×30 mm PDF label for a Niimbot printer.
- **Inventory** – bullets, brass, primers and powder; building a load can draw stock down, with a prompt if you are short.
- **Firearms** – barrel length, twist (entered as `1:x`), caliber, notes.
- **Performance** – range sessions per lot: velocity, group, score, position, notes.
  - Import a **ShotMarker** export (`.tar`) to get shot positions, scores, and a shot viewer with zoom, hover details, and stats for checked shots.
  - Import a **Garmin Xero** CSV for muzzle velocity. Shots are paired with ShotMarker shots by timing (falling back to velocity pattern), and you are asked when the shot counts or timing disagree.
  - Sessions with Garmin data but no ShotMarker string open a speed scatter plot (average and ±1 SD) in place of the target.
- **Settings** – theme, units, label contents, backup, and the running version.

Column pickers (per table, separate for wide and narrow screens), inventory filter chips, and the current tab (kept in the URL hash) are remembered by the browser or the database.

## Project structure

```
reloading-app/
├── app.py                         # Flask backend + REST API + label rendering
├── sm_decode.py                   # ShotMarker export decoder (tar → shots, scores, timestamps)
├── requirements.txt               # flask, gunicorn, reportlab
├── Dockerfile                     # python:3.12-slim image; APP_VERSION build arg
├── docker-compose.yml             # Full stack: app + nginx + Cloudflare Tunnel (built from source)
├── docker-compose.portainer.yml   # App only, pulls a pinned image from GHCR (used by Portainer)
├── nginx.conf
├── static/
│   └── index.html                 # The whole front end
├── tests/
│   ├── test_backend.py            # API and decoder checks, no packages needed (Flask is stubbed)
│   ├── frontend.test.js           # Pure-function checks lifted out of index.html, Node only
│   └── fixtures/                  # Real ShotMarker export and Garmin CSV used by the tests
└── .github/workflows/
    ├── ci.yml                     # Tests on pushes to main that touch code
    └── release.yml                # Tag push: tests, tarball release, image to GHCR, deploy pin
```

## Run locally

Python 3.10+ (the image uses 3.12).

```bash
pip install -r requirements.txt
python app.py            # creates reloading.db in the current directory
```

Open http://localhost:5000. Set `DB_PATH=/path/to/mydata.db` to keep the database elsewhere.

## Tests

```bash
python3 tests/test_backend.py
node tests/frontend.test.js
```

Neither needs any packages. GitHub Actions runs both on pushes to `main` that change code (docs-only pushes cost nothing) and again on every release tag before anything is published.

## Deploying

Releases are driven by version tags (`v57`, `v58`, …); there is no tarball to upload by hand.

1. Commit and push to `main`; CI runs the tests.
2. Push a tag `vNN`. `release.yml` then:
   - runs the tests (a failure publishes nothing),
   - attaches `reloading-app_NN.tar` and its SHA-256 to a GitHub release,
   - builds the image and pushes `ghcr.io/cody0303/reloading-app:vNN` (and `:latest`) with the version baked in (shown in Settings),
   - commits the new image pin to the `deploy` branch, which holds only `docker-compose.portainer.yml`.
3. Portainer (Git stack on the `deploy` branch, polling) sees the new commit and redeploys the pinned image within a couple of minutes.

Notes:
- Images are always pinned to a version, never `:latest`; Portainer CE cannot force a re-pull.
- The repository and the GHCR package are private, so Portainer needs a GitHub token for cloning and a `ghcr.io` registry login (a token with `read:packages`).
- The database lives in a Docker volume (`reloading-app-data` on the Portainer host, `reloading-data` in the full compose file). Redeploying keeps it. **Never run `docker compose down -v`**: `-v` deletes the volume and every record in it.

### Building from source instead

```bash
docker compose up -d --build     # app + nginx + cloudflared (needs TUNNEL_TOKEN in the environment)
```

Without a tunnel, use the Portainer compose file or add a `ports:` mapping to the app service.

## Backup & restore

**Online backup:** `GET /api/backup` returns a consistent copy of the SQLite database, even while the app is running. The endpoint has no authentication, so keep it on the LAN and never route it through the public tunnel.

```bash
curl -o reloading_backup.db http://<docker-host>:5002/api/backup
```

A nightly pull of that URL (for example from Home Assistant, whose Google Drive backup add-on then carries it off-site) is an easy automated setup. The database is small: roughly 3 KB per 22-shot session.

**Restore** by stopping the app, then copying the file over the volume's `reloading.db`:

```bash
docker run --rm -v reloading-app-data:/data -v "$(pwd)":/backup alpine cp /backup/reloading_backup.db /data/reloading.db
```

The Export CSV button in the app gives a portable text copy of the loads table only.

## Security

There is no login. Keep the app on your LAN or behind something that authenticates (Cloudflare Access, Tailscale). All data from the database is HTML-escaped before display.

## REST API

All endpoints return JSON unless noted.

| Area        | Endpoints |
|-------------|-----------|
| Loads       | `GET/POST /api/loads`, `PUT/DELETE /api/loads/{id}`, `GET /api/loads/new-lot`, `GET /api/loads/{id}/label` (PDF) |
| Inventory   | `GET/POST /api/inventory`, `PUT/DELETE /api/inventory/{id}`, `POST /api/inventory/{id}/add`, `POST /api/inventory/check`, `GET /api/inventory/names/{category}` |
| Firearms    | `GET/POST /api/firearms`, `PUT/DELETE /api/firearms/{id}` |
| Performance | `GET/POST /api/performance`, `PUT/DELETE /api/performance/{id}` |
| ShotMarker  | `POST /api/shotmarker/upload` (parse only), `GET/POST/DELETE /api/performance/{id}/shots` |
| Chronograph | `POST /api/chrono/upload` (parse only), `GET/POST/DELETE /api/performance/{id}/chrono` |
| Other       | `GET /api/stats`, `GET /api/distinct/{column}`, `GET/POST /api/settings`, `GET /api/version`, `GET /api/backup`, `GET /api/export`, `POST /api/import` |

`GET /api/loads` accepts `search`, `cartridge`, `powder_type`, `bullet_weight`, `annealed`, `sort` and `dir` (`ASC`/`DESC`).

```bash
curl "http://localhost:5000/api/loads?cartridge=308&sort=powder_weight&dir=ASC"
```
