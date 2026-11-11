#!/usr/bin/env bash
# Compare empty-volume startup and usable intake under identical hosting restrictions.
set -euo pipefail
baseline=${1:?baseline image required}
candidate=${2:?candidate image required}
artifacts=${3:?artifact directory required}
samples=${4:-10}
[[ "$samples" =~ ^[0-9]+$ ]] && (( samples > 0 ))
mkdir -p "$artifacts"
artifacts=$(cd "$artifacts" && pwd)
name="fresh-startup-$$"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
printf '%s\n' '{"workspace_id":"00000000-0000-4000-8000-000000000149","platform_url":"http://127.0.0.1:1","credential":"fixture-no-platform-access"}' > "$artifacts/bootstrap.json"
chmod 644 "$artifacts/bootstrap.json"
for sample in $(seq 1 "$samples"); do
  # Alternate order so later runs do not consistently favor the candidate.
  variants=(baseline candidate)
  if (( sample % 2 == 0 )); then variants=(candidate baseline); fi
  for variant in "${variants[@]}"; do
    image=$baseline
    if [[ "$variant" == candidate ]]; then image=$candidate; fi
    proof="$artifacts/$variant-$sample"
    mkdir "$proof"
    docker image inspect "$image" --format '{{.Id}} {{.Os}}/{{.Architecture}}' > "$proof/image.txt"
    docker volume create "$name" >/dev/null
    docker run --rm --user 0 --entrypoint /bin/sh -v "$name:/workspace" "$image" -c 'chown 10000:10000 /workspace'
    docker run -d --name "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
      --cpus 2 --memory 2g --tmpfs /tmp:rw,size=1g \
      --mount "type=volume,src=$name,dst=/workspace" \
      --mount "type=bind,src=$artifacts/bootstrap.json,dst=/run/workspace/bootstrap.json,readonly" "$image" >/dev/null
    ready=false
    deadline=$((SECONDS + 120))
    while (( SECONDS < deadline )); do
      if docker exec "$name" /opt/hermes/.venv/bin/python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8650/health", timeout=2)' >/dev/null 2>&1; then
        ready=true
        break
      fi
      test "$(docker inspect --format '{{.State.Running}}' "$name")" = true
      sleep 1
    done
    # Capture evidence before assertions, including failed starts.
    docker logs "$name" > "$proof/boot.log" 2>&1
    test "$ready" = true
    docker exec "$name" /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-intake.py > "$proof/intake.txt" 2>&1
    docker exec "$name" /opt/hermes/.venv/bin/python -c \
      'import json; from pathlib import Path; root=Path("/workspace/host"); print(json.dumps({str(p.relative_to(root)):p.read_text() for p in root.rglob("gateway.log")}))' > "$proof/gateway.json"
    docker stop --time 30 "$name" >/dev/null
    test "$(docker inspect --format '{{.State.ExitCode}}' "$name")" = 0
    docker logs "$name" > "$proof/boot.log" 2>&1
    # SDK span timestamps, never the health observation loop, define measured durations.
    docker run --rm -i --entrypoint /opt/workspace-env/bin/python "$candidate" \
      /opt/distribution/image/system_tests/check-startup.py 1 < "$proof/boot.log" > "$proof/timings.txt"
    cat "$proof/timings.txt"
    cleanup
  done
done
