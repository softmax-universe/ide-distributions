"""Check our startup span coverage and boot correlation using real image logs."""

import json
import sys
from datetime import datetime
from statistics import median

expected = {
    ("workspace.startup", ""),
    *(
        (f"workspace.profile_{phase}", role)
        for role in ("orchestrator", "researcher")
        for phase in ("install", "prepare")
    ),
    *(
        (f"workspace.{phase}", "")
        for phase in ("gateway_ready", "application_wake", "application_ready")
    ),
}
boots: dict[str, list[dict]] = {}
for line in sys.stdin:
    # Real prints share this stream with telemetry, and one joined to its line would not parse.
    if '"service":"ide-agent-telemetry"' in line:
        json.loads(line)
        continue
    if not line.startswith('{"name": "workspace.'):
        continue
    span = json.loads(line)
    if (span["name"], span["attributes"].get("role", "")) not in expected:
        continue
    resource = span["resource"]["attributes"]
    assert resource["workspace.id"] == "00000000-0000-4000-8000-000000000149", span
    boots.setdefault(resource["service.instance.id"], []).append(span)

cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 2
assert len(boots) == cycles, f"Expected {cycles} boot IDs, got {list(boots)}"
samples: dict[tuple[str, str, str], list[float]] = {}
for index, (boot_id, spans) in enumerate(boots.items()):
    assert len(spans) == len(expected), spans
    assert {(s["name"], s["attributes"].get("role", "")) for s in spans} == expected, spans
    root = next(s for s in spans if s["name"] == "workspace.startup")
    state = "fresh" if index == 0 else "retained"
    assert root["attributes"] == {"workspace.state": state, "startup.complete": True}, root
    for span in spans:
        assert span["status"]["status_code"] != "ERROR", span
        if span["name"] == "workspace.profile_install":
            assert span["attributes"]["profile.updated"] is (index == 0), span
        if span is not root:
            assert span["context"]["trace_id"] == root["context"]["trace_id"], span
            assert span["parent_id"] == root["context"]["span_id"], span
        seconds = (
            datetime.fromisoformat(span["end_time"]) - datetime.fromisoformat(span["start_time"])
        ).total_seconds()
        samples.setdefault((state, span["name"], span["attributes"].get("role", "")), []).append(
            seconds
        )
    print(f"PASS startup spans: boot={boot_id} state={state} completed={len(spans)}")

for (state, phase, role), values in samples.items():
    print(f"{state} {phase} {role or '-'}: n={len(values)} median={median(values):.3f}s")
