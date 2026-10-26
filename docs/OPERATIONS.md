# Running MapExp

[Back to README](../README.md)

## Configuration

Copy `.env.example` and `sources.json.example` only in a new checkout. Preserve existing credentials, source lists, sessions, and data when upgrading.

| Setting | Purpose |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | Telegram application credentials. |
| `TG_SESSION` | Existing authorized Telethon session path (with or without `.session`). |
| `OPENROUTER_API_KEY` | Shared authentication for decisions and text synthesis. |
| `JEV_MODEL`, `OPENROUTER_MODEL` | Decision and text model IDs. Current defaults: `perplexity/pplx-decider-v1.2` and `deepseek/deepseek-v4.1-flash-1017`. `JEV_MODEL` retains its historical variable name. |
| `CITY_NAME`, `CITY_TIMEZONE`, `CITY_LAT`, `CITY_LON`, `CITY_ZOOM` | City scope, display timezone, and initial map. Set `Asia/Yekaterinburg` for Perm; the current example defaults to `Europe/Moscow`. |
| `DATABASE_PATH`, `MEDIA_DIR` | Local persistent storage. |
| `GEOCODER_USER_AGENT` | Identifying user agent with a real operator contact for public Nominatim. |

An existing external Telegram environment file can supply credentials:

```bash
TG_SESSION='/absolute/path/to/authorized/session' .venv/bin/python ingest.py \
  --watch --recent-hours 36 --telegram-env '/path/to/existing/.env'
```

Session authorization is a prerequisite; the collector does not send Telegram messages. Do not share the same session between simultaneously running clients without assessing Telethon session constraints.

## Docker

Export `TG_API_ID` and `TG_API_HASH` into the shell running Compose. For an external authorized session, set `TG_SESSION_DIR` to its directory and `TG_SESSION_NAME` to its filename without `.session`.

The image runs as UID/GID `1000:1000`. In a fresh checkout, prepare writable bind-mount directories before Compose starts (otherwise Docker can create root-owned directories):

```bash
mkdir -p data media sessions
sudo chown 1000:1000 data media
docker compose up --build -d
# Optional temporary public tunnel:
docker compose --profile share up -d tunnel
```

Existing database/media files must also be readable and writable by UID 1000. The selected Telegram session and its directory must allow UID 1000 to read/write the SQLite session and journals; check ownership before mounting an external session. Keep session permissions private rather than making credentials world-readable.

Compose mounts `data/`, `media/`, `sessions/`, and `sources.json`; credentials, sessions, and runtime data are excluded from the image. The web port binds to `127.0.0.1:8010`. Use an HTTPS reverse proxy for a permanent public endpoint. Run only one ingestion worker per database, including across local and container processes.

The worker installs live-message handling before backfill, processes newer historical posts first, and polls for new messages and accepted-post edits every 30 seconds. Polling matters because public-channel push delivery is not guaranteed without subscription. It also retries decisions and media downloads, and syncs available comments.

## Hosted deployment

[GitHub CI/CD, persistent storage, HTTPS and recovery](DEPLOYMENT.md) describe the
production setup. Development Compose remains separate from the release-image
configuration in `deploy/compose.production.yml`.

## Public API

| Route | Purpose |
| --- | --- |
| `/api/config` | City, timezone, initial map settings. |
| `/api/events` | Active event cards, message and source counts. |
| `/api/events/{id}` | Active incident detail and accepted source history. |
| `/api/stream` | SSE invalidation signals and heartbeat. |
| `/api/status` | Worker freshness, last success, queue, and processing state. |
| `/healthz` | HTTP/SQLite health; does not prove provider health. |
| `/media/...` | Media attached to active incidents; unsafe formats download as attachments. |
| `/docs` | FastAPI's generated API documentation. |

See [delivery scaling and regression checks](SCALING.md) for shared SSE polling,
feed caching, gzip/ETags, schema migration, and the local 1,000-client exercise.

The UI preserves its last feed during disconnection. Map failure does not prevent reading the feed. If an SSE heartbeat is absent, the client falls back to polling every 15 seconds; buffering proxies may trigger this behavior. A temporary tunnel is available only while its process runs.

## Backups and evidence

Before updating a populated instance, create a SQLite online backup and copy the matching media, configuration, and code. Restore a consistent set with all relevant processes stopped. Copying only the main SQLite file while WAL writes are active is not a reliable backup.

Accepted evidence snapshots survive source edits and event expiry. Expiry ends public visibility rather than deleting event history. Media cleanup belongs to ingestion and runs only when every associated active event has ended. GET requests and SSE do not perform cleanup.

Unassociated ignored posts have their source text and version records cleared; deduplication identifiers, fingerprints, and decision audits remain. Audits may retain generated excerpts or quoted evidence; previously accepted evidence is preserved. Comments add source context but do not independently drive event decisions. Old pending inputs outside the seven-day retry window do not automatically keep spending model calls.

## Screenshot provenance

README screenshots were captured on 2026-10-14 through BrowserSkill from the real FastAPI/Leaflet application running on loopback with an isolated temporary SQLite database. All five incidents are synthetic, the city header says `ДЕМО`, and source names identify demonstration content. No live ingestion, provider calls, personal media, or production database writes were used. Desktop, incident detail, and emulated mobile layouts are shown; mobile emulation is not physical-device verification.

The banner and policy chart are authored SVG assets. The policy chart uses factual-support constants from `llm.py` and minimum display durations from `storage.py`, not experimental performance data.
