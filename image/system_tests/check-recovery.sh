#!/usr/bin/env bash
# Inject a lost-receipt obligation and prove the normal supervisor/container restart clears it.
set -euo pipefail
image=${1:?image required}
name=${2:?fixture name required}
volume=${3:?retained volume required}
scratch=${4:?artifact directory required}
docker run -d --restart on-failure:3 --name "$name" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,size=1g \
  --mount "type=volume,src=$volume,dst=/workspace" \
  --mount "type=bind,src=$scratch/bootstrap.json,dst=/run/workspace/bootstrap.json,readonly" "$image"
ready() {
  docker exec "$name" /opt/hermes/.venv/bin/python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8650/health", timeout=2)' >/dev/null 2>&1
}
deadline=$((SECONDS + 240))
until ready; do (( SECONDS < deadline )); sleep 1; done
# Both upgrade cases must reconcile release content while preserving owner data.
docker exec -i "$name" /opt/hermes/.venv/bin/python - <<'PYTHON'
from pathlib import Path
source = Path("/opt/distribution")
for role in ("orchestrator", "researcher"):
    home = Path("/workspace/host/profiles") / role
    assert (home / "SOUL.md").read_bytes() == (source / role / "SOUL.md").read_bytes()
    assert (home / ".softmax-distribution-image").read_bytes() == (source / "image-id").read_bytes()
    assert (home / "memories/reuse-proof.txt").read_text() == "owner memory\n"
print("PASS profile reconciliation: missing/different image markers, owner memory preserved")
PYTHON
# Fault injection models a durable admission whose native acceptance reply was lost.
recovery_started=$SECONDS
docker exec -i "$name" /opt/workspace-env/bin/python - <<'PY'
from research_core import layout
from research_core.store import admissions, sessions
from research_core.store.actors import Actor
from research_core.store.db import Store
with Store.open(layout.from_env().store_path, kind="workspace") as store:
    actor = Actor.harness()
    sessions.register(store, actor, session_id="fault-lost-receipt", profile="orchestrator", kind="chat")
    op_id = admissions.enqueue(store, actor, key="fault-lost-receipt", session_id="fault-lost-receipt",
                              profile="orchestrator", text="Fault fixture; never replay", origin="client")
    with store.tx(actor) as tx:
        tx.execute("UPDATE client_admissions SET dispatch_started=1,resolution_required=1,error='Lost receipt' WHERE op_id=?", (op_id,))
PY
deadline=$((SECONDS + 240))
while true; do
  (( SECONDS < deadline ))
  restarts=$(docker inspect --format '{{.RestartCount}}' "$name")
  if (( restarts > 0 )) && ready; then break; fi
  sleep 1
done
test "$restarts" = 1
printf '{"container_restarts":%s,"recovery_elapsed_seconds":%s}\n' \
  "$restarts" "$((SECONDS - recovery_started))" > "$scratch/recovery-observation.json"
cat "$scratch/recovery-observation.json"
docker exec -i "$name" /opt/workspace-env/bin/python - <<'PY'
from research_core import layout
from research_core.store.db import Store
with Store.open(layout.from_env().store_path, kind="workspace") as store:
    row = store.one("SELECT * FROM client_admissions WHERE native_key='fault-lost-receipt'")
    assert row["status"] == "interrupted" and row["run_id"] is None, dict(row)
    assert row["resolution_required"] == 0 and row["execution_uncertain"] == 0, dict(row)
    assert row["runtime_id"] != store.get_meta("runtime_id"), dict(row)
    assert store.get_meta("runtime_recovery_count") == "1"
print("PASS automatic lost-receipt recovery: one runtime restart, no input replay")
PY
# Check the command path while awake, before sleep sign-off changes its preconditions.
docker exec -e HERMES_HOME=/workspace/host/profiles/researcher "$name" \
  /opt/hermes/.venv/bin/python /opt/distribution/image/system_tests/check-transitions.py \
  > "$scratch/transition-commands.json"
cat "$scratch/transition-commands.json"
docker exec -e HERMES_HOME=/workspace/host/profiles/orchestrator "$name" \
  /opt/workspace-env/bin/research workspace sleep --wait 0 > "$scratch/recovery-sleep.json"
docker logs "$name" > "$scratch/recovery.log" 2>&1
docker exec -i "$name" /opt/hermes/.venv/bin/python -c '
import sys
sys.path.insert(0, "/opt/distribution/image")
from runtime import RuntimeRecovery
events = [RuntimeRecovery.model_validate_json(line) for line in sys.stdin
          if line.startswith("{\"event\":\"workspace_runtime_recovery\"")]
assert len(events) == 1, events
assert events[0].completed_recoveries == 0, events
print("PASS recovery telemetry: one request, durable recovery count=1")
' < "$scratch/recovery.log"
docker stop --time 30 "$name" >/dev/null
docker rm "$name" >/dev/null
