#!/usr/bin/env bash
# Forced SSH command for the dedicated deployment key. No general shell access.
set -euo pipefail
umask 077
read -r operation revision extra <<< "${SSH_ORIGINAL_COMMAND:-}"
if [[ "$operation" == ping && -z "${revision:-}" ]]; then
  echo mapexp-deploy-ready
  exit 0
fi
if [[ "$operation" != deploy || ! "${revision:-}" =~ ^[a-f0-9]{40}$ || -n "${extra:-}" ]]; then
  echo 'Only deploy <40-character commit SHA> is accepted.' >&2
  exit 2
fi
root_dir=${MAPEXP_ROOT:-/srv/mapexp}
cd "$root_dir"
exec 9>.deploy.lock
flock -w 600 9
read -r registry_user
read -r registry_password
[[ -n "$registry_user" && -n "$registry_password" ]]
auth_dir=$(mktemp -d)
container_id=
cleanup() {
  if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null 2>&1 || true; fi
  rm -rf "$auth_dir"
}
trap cleanup EXIT
printf '%s\n' "$registry_password" | docker --config "$auth_dir" login ghcr.io -u "$registry_user" --password-stdin
unset registry_password
image="ghcr.io/ashromanov/mapexp:$revision"
docker --config "$auth_dir" pull "$image"
release_dir="$root_dir/releases/$revision"
mkdir -p "$release_dir"
container_id=$(docker create "$image")
docker cp "$container_id:/app/deploy/." "$release_dir/"
docker rm "$container_id" >/dev/null
container_id=
printf '%s\n' "$image" > "$release_dir/.image"
export MAPEXP_ROOT="$root_dir" MAPEXP_IMAGE="$image" MAPEXP_REVISION="$revision"
bash "$release_dir/deploy.sh"
