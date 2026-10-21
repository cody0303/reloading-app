# Reloading Log

A self-hosted reloading log: load recipes, component inventory, firearms, and range performance, with
ShotMarker target data and Garmin Xero chronograph files linked to each session. Flask + SQLite on the
back, a single-page vanilla JS front end, no build step.

- **Loads** – recipes with lot numbers, searchable and sortable; prints a 50×30 mm PDF label for a Niimbot printer.
- **Inventory** – bullets, brass, primers and powder; building a load can draw stock down, with a prompt if you are short.
- **Firearms** – barrel length, twist (entered as `1:x`), caliber, notes, and a round count for barrel life. Each entry is treated as a barrel (fitting a new barrel means adding a new entry). Its rounds are the ones it had before the app, plus its performance sessions (sighters included), plus match stages scored by hand, plus a log for anything fired elsewhere. Set an expected barrel life and the Rounds column turns amber at 80% and red at 100%.
- **Performance** – range sessions per lot: velocity, group, score, position, notes.
  - Import a **ShotMarker** export (`.tar`) to get shot positions, scores, and a shot viewer with zoom, hover details, and stats for checked shots.
  - Change a session's target face afterwards and its shots are scored again from their saved positions on the new face, with the same rule the ShotMarker software uses (a shot takes the first ring, working outwards, that its bullet reaches; a hit on the board outside every ring scores the board's value; off the board scores nothing). It reproduces ShotMarker's own scores exactly on the test data. Faces scored with an outline instead of rings are supported too: Target Rifle D / F, and the older "TRD", are scored by whether the bullet touches the outline, drawn in the viewer, and show a plain total. Those two follow the device software's rules but have not yet been checked against a real session. ShotMarker's score is never overwritten: the viewer, the Performance table and the session pickers show both, and a match stage takes the re-score.
  - Import a **Garmin Xero** CSV for muzzle velocity. Shots are paired with ShotMarker shots by timing (falling back to velocity pattern), and you are asked when the shot counts or timing disagree.
  - Sessions with Garmin data but no ShotMarker string open a speed scatter plot (average and ±1 SD) in place of the target.
- **Matches** – score tracking by course of fire.
  - Pick a course and its stages are copied into the match (editing a course later never changes matches already entered). Enter a score and an X count per stage by hand, or link a Performance session to the stage to fill them from its ShotMarker score. Scores read like ShotMarker's, for example `95-3X`.
  - Linking sessions also fills in the match date and the rifle. "Link sessions by date" pairs sessions within a day of the match with stages by distance, and the session pickers list sessions near the match date first. A session belongs to at most one match.
  - **Records** view: personal bests per stage, course averages and trends. The stage section has course chips (the pick is remembered), and stages sort by distance and then firing order. A best that came from a saved session has a button that opens that session's shots.
- **Settings** – theme, units, label contents, courses of fire (add, edit, reorder stages; six Highpower and mid-range courses are seeded to start with), backup, and the running version.

The app is laid out for phones as well as desktop: lists become cards and the tab bar shrinks to icons. Column pickers (per table, separate for wide and narrow screens), inventory filter chips, the Records course chip, and the current tab (kept in the URL hash) are remembered by the browser or the database.

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
│   ├── index.html                 # The whole front end
│   └── faces.js                   # Target-face ring tables, the one copy: the page loads it, and app.py reads it to re-score shots
├── tests/
│   ├── test_backend.py            # API and decoder checks, no packages needed (Flask is stubbed)
│   ├── frontend.test.js           # Pure-function checks lifted out of index.html, Node only
│   └── fixtures/                  # Real ShotMarker export and Garmin CSV used by the tests
└── .github/workflows/
    ├── ci.yml                     # Tests on pushes to main that touch code
    ├── release.yml                # Tag push: tests, tarball release, image to GHCR, deploy pin
    └── prune-images.yml           # Monthly: keep the newest 10 image versions in GHCR
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
- The image has a health check (it asks `/api/stats` every 30 seconds), so Portainer shows the container as healthy or unhealthy. Unhealthy is a warning only: Docker doesn't restart a container for it.
- The repository and the GHCR package are private, so Portainer needs a GitHub token for cloning and a `ghcr.io` registry login (a token with `read:packages`).
- The database lives in a Docker volume (`reloading-app-data` on the Portainer host, `reloading-data` in the full compose file). Redeploying keeps it. **Never run `docker compose down -v`**: `-v` deletes the volume and every record in it.

### Image cleanup

`prune-images.yml` runs on the 1st of each month and deletes all but the newest 10 versions of the image in GHCR (private storage is capped at 500 MB). It never deletes the version pinned on the `deploy` branch or the one tagged `latest`, and stops without deleting anything if it can't read the pin. Run it by hand from the Actions tab for a dry run (the checkbox is ticked by default) to see what it would remove. Versions older than the newest 10 can't be rolled back to without a rebuild.

### If Portainer stops updating

Portainer's polling job can stall without logging an error. Check the `deploy` branch's `docker-compose.portainer.yml` on GitHub: if it names the newest version, the pipeline did its part, and `docker restart portainer` (this restarts only Portainer, not your containers) gets it polling again. If the file is behind, look at the release run for the newest tag in the Actions tab.

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
| Firearms    | `GET/POST /api/firearms`, `PUT/DELETE /api/firearms/{id}`, `GET /api/firearms/{id}/rounds` (breakdown and log), `POST /api/firearms/{id}/log`, `DELETE /api/round-log/{id}` |
| Performance | `GET/POST /api/performance`, `PUT/DELETE /api/performance/{id}` |
| ShotMarker  | `POST /api/shotmarker/upload` (parse only), `GET/POST/DELETE /api/performance/{id}/shots` |
| Chronograph | `POST /api/chrono/upload` (parse only), `GET/POST/DELETE /api/performance/{id}/chrono` |
| Courses     | `GET/POST /api/courses`, `PUT/DELETE /api/courses/{id}` (a course is a name plus a list of stages) |
| Matches     | `GET/POST /api/matches`, `PUT/DELETE /api/matches/{id}` (each match carries its stages, with scores and linked session ids) |
| Other       | `GET /api/stats`, `GET /api/distinct/{column}`, `GET/POST /api/settings`, `GET /api/version`, `GET /api/backup`, `GET /api/export`, `POST /api/import` |

`GET /api/loads` accepts `search`, `cartridge`, `powder_type`, `bullet_weight`, `annealed`, `sort` and `dir` (`ASC`/`DESC`).

```bash
curl "http://localhost:5000/api/loads?cartridge=308&sort=powder_weight&dir=ASC"
```
