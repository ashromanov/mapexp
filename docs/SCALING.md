# MapExp delivery improvements

The current MapExp checkout includes the frontend rendering experiment and a
shared backend delivery path. API fields, ingestion decisions, location policy,
media permissions, and the last-feed/disconnection UX remain intact.

## Plan and implementation

1. **Replace full-history change detection.** SQLite AFTER triggers maintain a
   transactionally committed counter for event, membership, post, source, media,
   and evidence-version changes. No-op updates do not increment it. Worker-state
   heartbeats are excluded. A count of elapsed active events detects expiry even
   with ingestion stopped. Detection performs one SELECT instead of loading and
   hashing every incident history. The idempotent migration installs schema v2.
2. **Share SSE monitoring.** One asyncio polling task per Uvicorn process checks
   every two seconds. Subscribers wait on an in-memory condition; they do not
   query SQLite. Changes wake every subscriber; unchanged connections receive
   heartbeats every ten seconds. Database errors disconnect streams so clients
   can reconnect or use their existing polling fallback. Shutdown cancels the task.
3. **Batch and cache the feed.** Event/source retrieval uses two SELECTs. A read
   transaction pairs the revision and feed rows consistently. A process-wide lock
   allows one rebuild at a time. Pydantic validates once per rebuild, then cached
   JSON bytes serve subsequent requests. HTTP feed requests still perform one
   lightweight revision query, so they see committed changes without waiting for
   the background poll. Cache hits do not load histories or reserialize JSON.
4. **Reduce wire traffic.** Gzip bytes are built once with each snapshot, with
   representation-specific ETags and `Vary: Accept-Encoding`. Matching conditional
   GETs return bodyless 304 responses. Feed responses use `private, no-cache`;
   other API responses retain `no-store`. Static versioned assets use immutable
   caching; unversioned assets use a one-hour cache. SSE remains uncompressed.
5. **Update browser rendering and revalidation.** Cards clone a template, retain
   event identity, and update changed text. Leaflet markers/circles update in place;
   circles share Canvas. Search batches through animation frames; off-screen cards
   use content visibility. Cached formatting avoids repeated formatter creation.
   Mobile sheet resizing snaps to final geometry. Pointer capture stays on the
   sheet button so normal clicks still work. The browser sends its feed ETag;
   a 304 preserves pending updates and refreshes any open detail, including
   detail-only changes. Reconnect uses the initial SSE event for revalidation.
6. **Regress and measure.** Offline storage/pipeline/API tests, concurrent cache/SSE
   tests, mocked browser regressions, live browser smoke tests, and a real local
   1,000-connection HTTP/SSE exercise verify the result.

No production dependencies, Redis, CDN, proxy, or external tile provider were
added. Every Uvicorn process maintains its own cache and poller; SQLite provides
cross-process revision consistency. Changed feeds still transfer a complete
compressed snapshot. Incremental updates/pagination remain future work if feed
size or update frequency makes that necessary.

## Results

[Machine-readable measurements](performance-results.json) use 1,000 synthetic
incidents and 1,024 messages, with 800 map markers. Operation timings are medians
of three local runs, comparing the Git HEAD implementation with the new code on
the same database. Full feed and sampled detail payloads matched exactly.

| Operation | Before | After |
| --- | ---: | ---: |
| Feed SELECT statements | 1,001 | 2 |
| Feed query median | 48.078 ms | 28.481 ms |
| Change-detection SELECT statements | 5,049 | 1 |
| Change-detection median | 1,217.106 ms | 0.918 ms |

The real HTTP exercise held **1,000 SSE connections** while issuing feed requests
with **50 concurrent HTTP requests**. The following measurements use Python 3.14:

- All 1,000 connected, received heartbeats, and received the change notification.
- Idle monitoring performed six revision checks in twelve seconds, with no feed rebuild.
- One write rebuilt the feed once and notified all clients in 614.35 ms in this run.
- 1,000 conditional requests completed in 2.142 s; p95 was 120.97 ms, with zero body bytes.
- 1,000 full compressed requests completed in 4.089 s; p95 was 594.92 ms.
- Feed bytes decreased from 1,006,338 to 82,999 with gzip (about 92% smaller).
- Server peak resident memory was approximately 141 MiB. No request/subscriber errors occurred.

The full suite passed **65 tests on both Python 3.11.15 and Python 3.14**.
The HTTP/SSE exercise also passed separately on Python 3.11 with freshly installed
requirements: all 1,000 subscribers received updates, six idle checks ran in twelve
seconds, and one write caused one rebuild. That run delivered all notifications
in 1,544.78 ms; full-request p95 was 775.71 ms. Both runs had zero client errors.
Compilation, UI helper checks, and Ruff checks on changed Python files passed.

These measurements include local client overhead and are not a production SLA.
The HTTP exercise uses an isolated child server and temporary database; it does
not request map tiles, call providers, or mutate its input database.

## Reproduce

From the repository root with dependencies installed:

```bash
.venv/bin/python selftest.py
node static/ui.test.mjs
.venv/bin/python -m compileall -q settings.py storage.py geocode.py llm.py pipeline.py ingest.py web.py tools tests
.venv/bin/python tools/load_test.py --database data/render-comparison.sqlite3 --users 1000
```

The load-test command requires an existing populated test database and a free
local port (8022 by default, override with `--port`). It makes its own SQLite
online copy, instruments only the disposable server, tests conditional and full
compressed requests, broadcasts a write, reconnects, then shuts down and removes
its copy. The source database and running comparison servers are preserved.

Browser checks use a temporary development dependency:

```bash
npm install --prefix /tmp/mapexp-render-tests playwright-core
export MAPEXP_PLAYWRIGHT_MODULE=/tmp/mapexp-render-tests/node_modules/playwright-core/index.mjs
node static/render.test.mjs
# Against an already running populated instance, default http://127.0.0.1:8010:
node static/live.test.mjs
```

Chromium defaults to `/usr/bin/chromium` (override with `CHROMIUM_PATH`). Set
`MAPEXP_URL` for another live instance. For Firefox/WebKit, install the corresponding
Playwright browsers and host libraries, then set `MAPEXP_BROWSER=firefox` or
`MAPEXP_BROWSER=webkit`. Mocked browser checks use local fixtures and mocked tiles;
live smoke checks use the real API, SSE, compression, and conditional requests.
Chromium and Firefox were exercised at desktop and mobile viewport sizes. WebKit
could not launch on this Artix host because its required Ubuntu shared libraries
were missing; Safari and physical mobile devices were not tested.

Before starting updated code against existing data, make an SQLite online backup.
The schema migration adds a table and triggers, retains evidence and IDs, and is
covered by legacy-schema migration tests. GET/SSE reads do not expire events,
delete evidence, or clean up media. Existing authorization and path checks remain.

References: [SQLite triggers](https://www.sqlite.org/lang_createtrigger.html),
[HTTP conditional requests](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/If-None-Match).
