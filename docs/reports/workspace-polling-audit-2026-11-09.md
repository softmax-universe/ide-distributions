# Workspace polling audit — November 9, 2026

Scope: the hosted distribution at `96ea0db6`, especially `image/` and the application/store/watcher
paths in `core/src/research_core/`. The platform observations below are separate; they are not
changes to the distribution. Test-harness probes are excluded from production recommendations.

The incident's runtime-stage spans are in
[Datadog](https://app.datadoghq.com/apm/trace/08bd284bfd53b2f9c397743a0e84ace1).
Profile update commands consumed 4.573 seconds, preparation 1.440 seconds, gateway readiness 4.066
seconds, application wake 1.564 seconds and application readiness 2.037 seconds. These are enclosing
stage durations, not measurements of interpreter overhead or polling penalties. Inference startup
already overlapped gateway startup. No production speedup is established by this audit.

## Changes in this branch

- Skip full profile reconciliation when the installed profile's successful image marker matches
  the running image. The upstream command unconditionally recopies distribution-owned files and
  updates `installed_at`; it does not compare revisions. Preparation and credential refresh remain.
- Replace gateway and application one-second HTTP polling with private Unix datagram notifications
  emitted by their listener owners. Keep one HTTP contract check, child-exit detection and deadlines.
  The gateway signal is a packaged patch against our pinned Hermes, not an upstream dependency.

## Remaining production loops

| Owner and location | Current mechanism | Replacement / reason to retain |
|---|---|---|
| `image/runtime.py`, `reconcile_uncertain_execution` | GET `/status` every second to detect `restart_required` | Have the application signal its supervisor when it commits an uncertain execution. Reuse an owned IPC channel or exit with a designated restart result after durable recording. Preserve the recovery fence and never replay uncertain inputs. No need for periodic HTTP here. |
| `serve.py`, `dispatch_forever` and `coordinate_forever` | In-process condition plus a 500 ms fallback | Publish committed-store change notifications across processes, not only threads. Keep SQLite cursors as truth, subscribe before reading and re-read after wake. Schedule explicit deadlines for `retry_at`, holds and inference expiry. Removing the fallback now can strand tool subprocess writes and timed work. |
| `serve.py`, `_stream` | In-process condition plus 500 ms database tail | Same cross-process notification mechanism. Preserve cursor replay, reconnect handling and a keepalive timer. Current code captures `change_version()` after reading rows: a commit in between is missed until the timeout. Capture before the read when implementing the subscription. |
| `serve.py`, `observe_forever`; `coordinator.py`, `reconcile` | Periodic reconciliation, currently a 60-second watcher interval | Native runs already expose event streams. Feed their terminal events to the coordinator; retain a slower repair pass for gaps/restarts. External tournament/job observations still need a provider watch or explicit polling policy. Do not confuse native completion with the later observed terminal state. |
| `watcher/sleep.py`, `settle_orchestrator_runs`; `watcher/drain.py`, `wait_quiet` | One-second checks of native run status | Await native terminal events with an initial snapshot and replay cursor; retain the drain deadline and stop escalation. Must cover both orchestrator and researcher runs. |
| `watcher/sleep.py`, `wait_for_processes` | One-second PID liveness checks | Owned children should use process wait; Linux non-child processes can use pidfds. PID-only polling also needs identity protection against PID reuse. This is a sleep/drain path, not initial wake latency. |
| `locks.py`, `file_lock` | Nonblocking file-lock attempts every 50 ms | A bounded, cancellable kernel lock wait could remove these retries. The dispatch lock already uses blocking `flock`; do not change it on the assumption that it polls. |
| `transitions.py`, `_await_result` | 100 ms reads of command receipts and interruption status | Wait on cross-process committed changes and an interruption signal, keeping the bounded pending-receipt return. The newly allocated `threading.Event` is never signalled; its wait is just a sleep. |
| `client.py`, `wait_admission` / `wait_run` | Snapshot every 100 ms / native run status every 200 ms by default | Use existing workspace/native streams with initial snapshots and durable cursors. These are optional synchronous client waits, not the browser startup gate. |
| `store/cli.py`, event follow | Re-read rows at the requested interval | Share the cross-process change subscription while retaining the command's cursor and termination semantics. |
| `watcher/sleep.py`, `flush_everything`; `store/migrate.py`, `_enable_wal` | Bounded database checkpoint/lock-contention retries | Preserve until SQLite exposes a usable completion notification or writes are serialized through an owner. These retries respond to contention and are not unconditional startup delay. |
| `image/aide.py`, `consume` after a quiet closed native stream | One-second re-read/re-follow while the request is active | Await the existing request wake event when the seam owns the next transition. Native replay exhaustion/disconnection must be distinguished from completion so a missing event cannot strand the consumer. |
| `image/aide.py`, stream reconnect; `image/control.py`, relay reconnect | One-second retry following transport failure | Retain bounded/backoff retry: a disconnected remote cannot deliver the event that reconnects it. This is failure recovery, not steady-state polling. |
| `image/inference.py`, `maintain` | Sleep until lease renewal/refill, plus refusal/transport backoff | Retain explicit deadlines and Retry-After. This is scheduled renewal. Readiness already uses an `asyncio.Event`; it does not poll for the first key. |
| `image/aide.py`, progress flush | Wait on the next stream event with a batching deadline | Retain: the timer flushes buffered text; it is not a readiness check. |
| `serve.py`, HTTP server `poll_interval=0.2` | Standard library server shutdown check | Not a 200 ms request dispatch delay. A selector wakes for socket activity. Replace only if changing server ownership/shutdown architecture, not as a startup optimization. |

The platform's `observatory_api/clients/hermes/lifecycle.py::wait_available` polls backend
observations every two seconds. Kubernetes supports watches, so its implementation can subscribe
from a resource version and wake on pod changes. Preserve intent-revision/supersession checks and
relist after an expired watch. Other backends need their own wait contract. The five-second deletion
loop in `kubernetes.py` is similarly watchable but is outside the wake path.

The incident browser retried session opening at about two-second intervals. A platform readiness
subscription could remove that observation delay, but it requires a cross-service contract change.
It is not fixed by an internal gateway signal, and this branch does not claim to change it.

## Order of work

1. Measure the packaged unchanged-image fast path and both listener signals on fresh and retained
   volumes, including missing-marker/upgrade recovery and a child exiting before readiness.
2. Replace the supervisor's recovery status polling with an application-owned transition signal.
3. Add durable-cursor-backed cross-process store notifications and explicit next-deadline scheduling.
   Then remove the 500 ms fallback loops together; replacing just one consumer leaves the same lag.
4. Connect native completion streams to coordinator/drain and platform watches to compute readiness.
   Keep repair/reconnect paths explicit and test missed events and restarts.

This audit does not recommend replacing every `sleep` with an event. Retry delays, lease deadlines,
stream batching and keepalives are timers with defined work. Repeatedly asking an owned component
whether it has completed a deterministic transition is the avoidable pattern.
