# GitHub CI/CD and hosting

The public service is configured at https://mapexp.2-26-150-51.sslip.io.
GitHub hosts the repository, Actions pipeline and container registry. The Python
API and Telegram worker run on an existing Ubuntu VPS, behind its existing Caddy
service. GitHub Pages does not run the backend.

## Production reference (verified 2026-10-26)

| Item | Current value |
| --- | --- |
| Repository / deployment branch | `git@github.com:ashromanov/mapexp.git` / `main` |
| Public map | https://mapexp.2-26-150-51.sslip.io |
| VPS / SSH | `2.26.150.51:22`, Ubuntu 26.04; operator login `root`, CI login `mapexp-deploy` |
| Resources | 1 vCPU, approximately 2 GiB RAM |
| Runtime | Docker Compose project `mapexp`, containers `mapexp-web-1` and `mapexp-ingest-1` |
| Persistent root | `/srv/mapexp` |
| Reverse proxy | Caddy, `/etc/caddy/mapexp.caddy` → `127.0.0.1:8010` |
| Text model, `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash-1017` |
| Decision model, `JEV_MODEL` | `perplexity/pplx-decider-v1.2` (historical variable name) |
| City | Пермь; `Asia/Yekaterinburg`; center `58.0105, 56.2502`, zoom `11` |
| Sources | `chp_59_perm`, `chp_159_59`; authoritative list in `/srv/mapexp/sources.json` |
| Collection | One worker, `--watch --recent-hours 168`, polling every 30 seconds |

GitHub Actions is the active deployment system; GitLab is not used. The old
`191.96.11.55` address is not the deployed MapExp server. Existing VPN/Xray and
Caddy services share this VPS, so preserve their configuration during maintenance.
The retired `MapExp-fast-render` worktree was removed; develop in the main MapExp
repository.

Operator SSH from the existing workstation (the private key stays outside Git):

```bash
ssh -i ~/.ssh/vpn_primary_ed25519 -o IdentitiesOnly=yes \
  -o BatchMode=yes -o StrictHostKeyChecking=yes root@2.26.150.51
```

The model-switch release was `b27515996db83a24e2f1f1019de049f0632a0567`, verified
by [successful CI and production deployment](https://github.com/ashromanov/mapexp/actions/runs/40366702885).
This is a historical reference, not a pin: query `/healthz` and `current` for the
release actually running now.

## Pipeline

`.github/workflows/deploy.yml` runs on pushes to `main`, pull requests and manual
workflow dispatches:

1. Run the complete offline regression suite on Python 3.11 and 3.14, using hashed
   dependencies from `requirements.lock`.
2. Exercise the browser UI with 1,000 synthetic incidents in Chromium, Firefox
   and WebKit. Test artifacts contain synthetic data only.
3. Exercise 1,000 simultaneous real SSE connections and HTTP requests against a
   disposable database/server. Telegram, models and map tiles are not called.
4. After those checks, build the production image, start it with an empty database,
   and check health, build revision, HTML asset versions, gzip, ETags and SSE.
5. Publish that exact tested image to `ghcr.io/ashromanov/mapexp:<commit SHA>`.
6. On `main` only, deploy through the `production` environment and check public
   HTTPS against the exact commit, including gzip/conditional GET/SSE behavior.

Actions are pinned to commit SHAs. Pull requests cannot publish or deploy. The
production environment permits only `main`, with no manual approval required.
Deployments are serialized. Static asset URLs are versioned with the image's commit
SHA to prevent a long-lived browser cache from hiding frontend updates.

## Persistent runtime

`/srv/mapexp` on the server contains:

- `.env`: private Telegram/OpenRouter credentials and city/model configuration.
- `sources.json`: the configured public-channel allowlist.
- `data/mapexp.sqlite3`, its WAL files, and `data/backups/`.
- `media/` and `sessions/mapexp.session`.
- `releases/<SHA>/`: deployment scripts, Compose definition and immutable image reference.
- `current`: symlink to the last healthy release.
- `deploy/remote-command.sh`: the root-owned forced SSH entrypoint.

`deploy/compose.production.yml` runs a read-only, non-root web container and one
non-root ingestion container. Writable data/media/session paths are bind-mounted;
they are never baked into an image. Web receives only public city/storage settings;
provider keys and Telegram sessions are available only to ingestion. Each container
has a 512 MiB memory limit and bounded logs. The web port binds only to loopback.
The worker watches live channels and backfills the last 168 hours with normal
source-revision, verification and expiry rules.

Caddy's existing VPN site and routes remain unchanged. An additional hostname in
`/etc/caddy/mapexp.caddy` proxies to `127.0.0.1:8010`. The example is
`deploy/Caddyfile.example`. SSE is not compressed or buffered. A real custom domain
can replace the temporary IP-derived hostname after updating DNS/Caddy and the
GitHub `DEPLOY_URL` variable.

## Deployment credentials

The `production` GitHub environment contains these non-secret variables:

| Name | Meaning |
| --- | --- |
| `DEPLOY_HOST` | `2.26.150.51` |
| `DEPLOY_USER` | Dedicated `mapexp-deploy` account |
| `DEPLOY_PORT` | `22` |
| `DEPLOY_URL` | `https://mapexp.2-26-150-51.sslip.io` |

It contains only two deployment secrets:

- `DEPLOY_SSH_KEY`: a dedicated key, not the operator's personal SSH key.
- `DEPLOY_KNOWN_HOSTS`: pinned, previously verified server host keys.

The corresponding authorized key uses
`restrict,command="/srv/mapexp/deploy/remote-command.sh"`. It accepts only
`deploy <40-character SHA>` or `ping`; arbitrary shell commands, forwarding and
PTYs are disabled. The deployment account can use Docker. Registry access uses the
job's short-lived `GITHUB_TOKEN` via SSH stdin and an ephemeral Docker auth directory;
no permanent registry token is installed on the VPS.

Provider credentials, real posts, databases, media, and Telegram session files
are excluded from Git and Docker build contexts. Set these up on the server through
an existing trusted SSH connection. `deploy/production.env.example` contains only
placeholders. Use a dedicated authorized Telegram session and avoid concurrent
clients against the same session from different hosts.

## Release transaction and recovery

The forced command pulls the tested SHA, extracts its deployment files and holds a
server-side deployment lock. The release script stops ingestion, creates an SQLite
online backup, updates and waits for a healthy web container, then starts and waits
for the ingestion heartbeat. Only then does it switch `current` to the new release.

A failed readiness check attempts to recover the previous application image and
worker. Regression tests verify backup ordering, initial deployment, rollback, and
rejection of injected/arbitrary SSH commands. Application rollback retains the
current database and therefore assumes backward-compatible migrations. Backups are
preserved for deliberate restoration; automatic restoration would discard evidence.

To redeploy a previously cached release through the operator's trusted SSH login:

```bash
mapexp_revision=PREVIOUS_COMMIT_SHA
mapexp_release="/srv/mapexp/releases/$mapexp_revision"
MAPEXP_ROOT=/srv/mapexp MAPEXP_REVISION="$mapexp_revision" \
  MAPEXP_IMAGE="$(cat "$mapexp_release/.image")" \
  flock /srv/mapexp/.deploy.lock bash "$mapexp_release/deploy.sh"
```

## Operations

### Ship a code update

Commit and push to `main`; the pipeline tests, publishes and deploys automatically.
Use the Actions page to follow the release; a successful push alone does not mean
production has updated. Manual reruns are also available:

```bash
gh run list --workflow deploy.yml --limit 5
gh workflow run deploy.yml --ref main
```

### Change models or other private runtime settings

Edit `/srv/mapexp/.env` on the VPS, preserving its credentials and private file
permissions. Server environment values override image defaults; editing
`settings.py` or the example env files alone does not override the production env.
A container restart does not reload its environment: recreate ingestion after an
env change, or let the next normal deployment recreate it. For an env-only change,
run this on the VPS, serialized with deployments:

```bash
(
  flock -x 9
  export MAPEXP_ROOT=/srv/mapexp
  export MAPEXP_IMAGE
  MAPEXP_IMAGE=$(cat /srv/mapexp/current/.image)
  docker compose --project-name mapexp --env-file /srv/mapexp/.env \
    -f /srv/mapexp/current/compose.production.yml \
    up -d --force-recreate --wait --wait-timeout 180 ingest
) 9>/srv/mapexp/.deploy.lock
```

This command applies ingestion settings; city settings supplied to `web` also
require recreating `web` or a normal deployment. Never start another worker against
the production database/session. Changing models affects subsequent processing;
it does not automatically regenerate existing cards.

### Check the deployed service

```bash
# Public deployment smoke check; optionally add --expected-revision COMMIT_SHA.
python tools/smoke_http.py https://mapexp.2-26-150-51.sslip.io
# On the VPS:
docker ps --filter label=com.docker.compose.project=mapexp
curl -fsS http://127.0.0.1:8010/healthz
curl -fsS http://127.0.0.1:8010/api/status
readlink -f /srv/mapexp/current
docker logs --tail 50 mapexp-ingest-1
# Print only effective model IDs, never the complete environment:
docker exec mapexp-ingest-1 python -c \
  'from settings import settings; print(settings.text_model); print(settings.decision_model)'
```

In `/api/status`, check `connected`, a fresh `heartbeat`, `provider_blocked`,
`pending`, `errors` and `last_error`. A healthy HTTP container does not prove that
Telegram collection or model calls work. The browser receives SSE update signals;
new cards appear when the user applies the update prompt or reloads the page.

Check workflow results at https://github.com/ashromanov/mapexp/actions.
Back up the SQLite database using its online backup API and preserve matching media,
session and configuration files. Do not copy a live SQLite file without its WAL.
Ingestion freshness/provider availability is separate from HTTP health.
