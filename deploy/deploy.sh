#!/usr/bin/env bash
# Called under the remote-command lock after the tested image has been pulled.
set -Eeuo pipefail
root_dir=${MAPEXP_ROOT:-/srv/mapexp}
release_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${MAPEXP_IMAGE:?Missing image}" "${MAPEXP_REVISION:?Missing revision}"
[[ "$MAPEXP_REVISION" =~ ^[a-f0-9]{40}$ ]]
previous_dir=$(readlink -f "$root_dir/current" 2>/dev/null || true)
previous_image=
if [[ -n "$previous_dir" && -f "$previous_dir/.image" ]]; then
  previous_image=$(cat "$previous_dir/.image")
else
  previous_dir=
fi
compose() {
  docker compose --project-name mapexp --env-file "$root_dir/.env" -f "$1/compose.production.yml" "${@:2}"
}
rollback() {
  status=$?
  trap - ERR
  echo 'Release failed; recovering the previous application image.' >&2
  compose "$release_dir" logs --tail 50 web ingest >&2 || true
  if [[ -n "$previous_image" ]]; then
    export MAPEXP_IMAGE="$previous_image"
    compose "$previous_dir" up -d --wait --wait-timeout 180 web ingest || true
  else
    compose "$release_dir" stop ingest || true
  fi
  exit "$status"
}
trap rollback ERR
# Stop the writer before an online SQLite backup or a schema migration.
compose "${previous_dir:-$release_dir}" stop --timeout 90 ingest
docker run --rm --network none -v "$root_dir/data:/app/data" --entrypoint python "$MAPEXP_IMAGE" -c '
import sqlite3
from pathlib import Path
from datetime import UTC,datetime
source=Path("/app/data/mapexp.sqlite3")
if source.exists():
    folder=source.parent/"backups";folder.mkdir(exist_ok=True)
    target=folder/("before-"+datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")+".sqlite3")
    with sqlite3.connect(source) as src,sqlite3.connect(target) as dst: src.backup(dst)
    print("Database backed up:",target.name)
'
compose "$release_dir" up -d --wait --wait-timeout 180 web
compose "$release_dir" up -d --wait --wait-timeout 180 ingest
curl --fail --silent --show-error http://127.0.0.1:8010/healthz
ln -sfn "$release_dir" "$root_dir/current"
trap - ERR
printf '\nDeployed %s\n' "$MAPEXP_REVISION"
