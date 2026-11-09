#!/usr/bin/env bash
# Protect startup and retained-volume recovery under the hosting filesystem restrictions.
set -euo pipefail
image=${1:?immutable or locally built image reference required}
name="distribution-smoke-${GITHUB_RUN_ID:-local}-$$"
volume="$name"
cycles=${STARTUP_CYCLES:-2}
[[ "$cycles" =~ ^[0-9]+$ ]] && (( cycles >= 2 ))
if [[ -n "${STARTUP_ARTIFACTS:-}" ]]; then
  mkdir "$STARTUP_ARTIFACTS"
  scratch=$(cd "$STARTUP_ARTIFACTS" && pwd)
else
  scratch=$(mktemp -d)
fi
docker image inspect "$image" --format '{{.Id}} {{.Os}}/{{.Architecture}}' > "$scratch/image.txt"
git rev-parse HEAD > "$scratch/source-revision.txt"
cleanup() {
  docker logs "$name" > "$scratch/container.log" 2>&1 || true
  cat "$scratch/container.log"
  docker logs "$name-aide" > "$scratch/aide.log" 2>&1 || true
  docker logs -t "$name-recovery" > "$scratch/recovery-container.log" 2>&1 || true
  docker rm -f "$name" "$name-recovery" "$name-aide" >/dev/null 2>&1 || true
  docker volume rm "$volume" "$volume-aide" >/dev/null 2>&1 || true
  if [[ -z "${STARTUP_ARTIFACTS:-}" ]]; then rm -rf "$scratch"; fi
}
trap cleanup EXIT
printf '%s\n' '{"workspace_id":"00000000-0000-4000-8000-000000000149","platform_url":"http://127.0.0.1:1","credential":"fixture-no-platform-access"}' > "$scratch/bootstrap.json"
chmod 644 "$scratch/bootstrap.json"
docker volume create "$volume" >/dev/null
# Kubernetes supplies this volume group ownership through fsGroup.
docker run --rm --user 0 --entrypoint /bin/sh -v "$volume:/workspace" "$image" -c 'chown 10000:10000 /workspace'
sentinel="retained-$name"
for cycle in $(seq 1 "$cycles"); do
  docker run -d --name "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
    --cpus 2 --memory 2g \
    --tmpfs /tmp:rw,size=1g --mount "type=volume,src=$volume,dst=/workspace" \
    --mount "type=bind,src=$scratch/bootstrap.json,dst=/run/workspace/bootstrap.json,readonly" "$image"
  ready=false
  deadline=$((SECONDS + 240))
  while (( SECONDS < deadline )); do
    if docker exec "$name" /opt/hermes/.venv/bin/python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8650/health", timeout=2)' >/dev/null 2>&1; then
      ready=true
      break
    fi
    test "$(docker inspect --format '{{.State.Running}}' "$name")" = true
    sleep 2
  done
  test "$ready" = true
  docker exec "$name" /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-intake.py
  docker exec "$name" /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-terminal.py
  docker exec "$name" /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-session-coworld.py
  if [[ "$cycle" == 1 ]]; then
    docker exec "$name" sh -c 'printf %s "$1" > /workspace/.state-proof' sh "$sentinel"
  else
    test "$(docker exec "$name" cat /workspace/.state-proof)" = "$sentinel"
  fi
  docker exec "$name" /opt/hermes/.venv/bin/python -c \
    'import json, urllib.request; s=json.load(urllib.request.urlopen("http://127.0.0.1:8650/status")); assert not s["sleep_started"] and not s["paused"], s'
  docker exec -e HERMES_HOME=/workspace/host/profiles/orchestrator "$name" \
    /opt/workspace-env/bin/research workspace sleep
  docker exec "$name" /opt/hermes/.venv/bin/python -c \
    'import json, urllib.request; s=json.load(urllib.request.urlopen("http://127.0.0.1:8650/status")); assert s["sleep_started"] and s["paused"], s'
  if [[ -n "${STARTUP_ARTIFACTS:-}" ]]; then
    docker exec "$name" /opt/hermes/.venv/bin/python -c \
      'import json; from pathlib import Path; root=Path("/workspace/host"); print(json.dumps({str(p.relative_to(root)): p.read_text() for p in root.rglob("gateway.log")}))' \
      > "$scratch/gateway-$cycle.json"
  fi
  docker stop --time 30 "$name" >/dev/null
  test "$(docker inspect --format '{{.State.ExitCode}}' "$name")" = 0
  docker logs "$name" > "$scratch/boot-$cycle.log" 2>&1
  cat "$scratch/boot-$cycle.log"
  docker rm "$name" >/dev/null
done

for cycle in $(seq 1 "$cycles"); do cat "$scratch/boot-$cycle.log"; done | docker run --rm -i --read-only \
  --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=64m \
  --entrypoint /opt/hermes/.venv/bin/python "$image" /opt/distribution/image/system_tests/check-startup.py "$cycles" | tee "$scratch/timings.txt"

# Fault-path component proof uses the packaged control connection and real HTTP/websocket peers in its own container.
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=64m \
  --entrypoint /opt/hermes/.venv/bin/python "$image" /opt/distribution/image/system_tests/check-transport.py

# Lease acquisition follows the platform retry contract before declaring inference ready.
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=64m \
  --entrypoint /opt/hermes/.venv/bin/python "$image" /opt/distribution/image/system_tests/check-inference.py

# Rare uncertain starts recover inside the image; no operator or forced platform hold is needed.
bash image/system_tests/check-recovery.sh "$image" "$name-recovery" "$volume" "$scratch"

# One workspace serves both protocols: the check plays the platform (lease, hosting control and
# the AIDE backend socket) and a scripted model, then drives an AIDE chat round trip.
printf '%s\n' '{"workspace_id":"00000000-0000-4000-8000-000000000150","platform_url":"http://127.0.0.1:8700","credential":"fixture-aide"}' > "$scratch/aide-bootstrap.json"
chmod 644 "$scratch/aide-bootstrap.json"
docker volume create "$volume-aide" >/dev/null
docker run --rm --user 0 --entrypoint /bin/sh -v "$volume-aide:/workspace" "$image" -c 'chown 10000:10000 /workspace'
docker run -d --name "$name-aide" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,size=1g --mount "type=volume,src=$volume-aide,dst=/workspace" \
  --mount "type=bind,src=$scratch/aide-bootstrap.json,dst=/run/workspace/bootstrap.json,readonly" "$image"
docker exec "$name-aide" /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-aide.py 8700 8701
