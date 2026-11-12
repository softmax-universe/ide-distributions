# Workspace orchestration and sleep ownership

The workspace coordinator owns execution admission, task transitions, recovery, and release.
The Orchestrator selects tasks, worker profiles, context, and rationale. Workers report results
through durable commands. Neither model selects replacement execution identities.

**Status, November 12, 2026:** implemented. It landed as PR #146 on November 3, 2026
(Pacific), with later changes in #156 and #180. Its links and the summary in `docs/hosting.md`
("Coordinator and pause") agree with the code; the spec was not re-checked line by line.

This is the implemented lifecycle contract. Merging relevant distribution changes to `main`
triggers image publication and the hosted qualification pipeline. These changes are active
through that release path; they are not a dormant migration.

## Authority and durable boundaries

The server acquires its lifetime ownership lock and publishes its coordinator instance before
HTTP or dispatch starts. HTTP and CLI status read that same instance. A contending server cannot
publish itself or serve requests. An immediate dispatcher failure propagates and releases ownership.

| Operation | Durable record | Next action owner |
| --- | --- | --- |
| Chat message | Client receipt plus delivery intent | Admission dispatcher |
| Assignment | Task revision, profile, context, rationale, session, and capacity reservation | Coordinator |
| Native start | Immutable key and payload; authentication scope, runtime identity, and replay horizon | Native transport, then coordinator |
| Worker report or human task edit | Coworld command, caller identity, lease epoch, and issuing attempt | Coordinator |
| Retry or resume | Predecessor disposition, deterministic successor, and assignment pointer | Coordinator |
| Release | Release request and execution obligations | Coordinator, or runtime supervisor when exclusion is required |

`client_admissions.op_id` identifies an input/execution attempt. Task identity and assignment
identity survive retries; execution sessions do not. `task_assignments.current_attempt_id`
identifies the authoritative worker attempt. A successor receives the durable task record,
worktree, branch, pending inputs, previous outcome, and bounded recent transcript.

An assignment's Task revision is the one its session last saw: `task_views` records what each
Orchestrator session was given by `task` results and the planning wake's text. `task.assign` fixes
that revision when its command is accepted and is refused if the Task differs when it is applied.
A session that has seen no revision gets the current one. The assignment's own identity is its
command's id.

Task ownership is recorded before native start. Holder transfer requires the expected previous
holder and advances the lease epoch. Lease age alone never proves that execution stopped.
Admission locking precedes the Coworld writer when both boundaries are needed.

## Commands and worker reports

Mutating native tools take no identity from the model. A call with the same fields, role, session
and lease epoch as a command whose finished result has not yet been returned to that session is a
retry: it joins that command, and if the work was already done it says so in
`repeat_of_earlier_call`; a refusal comes back as it was. Once the result has been returned (by the
call or by `task.command`), an identical call is a new intent, so a deliberate second message and a
call after a refusal both run. This is `transitions.request_generated`. A retry whose fields differ
is a new intent, which the domain rules bound: a second assignment or completion is refused, a
reworded message is sent. An interrupt (the user's stop, for one) ends a call's wait with its
pending receipt, which marks nothing, so its retry joins too; so does the retry of a call that
waited three seconds or more for the admission lock, because Hermes may have given up on it. Marking
happens in the tool call, before the model reads the reply, which leaves these gaps, where a retry
would apply as new: a gateway that stops between marking a result returned and Hermes saving it; an
interrupt that arrives just after the wait ends while marking waits over three seconds for the
store's write lock (Hermes has given up on the call by then); an interrupted
`workspace.change_complete`, whose subprocess cannot see the interrupt; and one model response
holding both `task.command` and a retry of that command. Likewise a `task.read` and a `task.assign`
in one response check the assignment against the revision that read just returned.

An explicit `operation_id` is still accepted, without being offered in any schema, because a chat
keeps the tool schema it started with. Internal callers (capability reports, platform waits,
human store edits) use it too. Repeating such an ID returns the same durable receipt; changing its
payload or caller epoch is refused.

Acceptance remains durable when the coordinator is unavailable. The caller waits briefly and
can receive `transition_pending`, with `task.command` to read the result; calling again with the
same fields joins the command. Commands have no expiry or coordinator retry cutoff. Unexpected
coordinator faults preserve accepted intent, record the error, and schedule another attempt with
bounded backoff. The retry count is diagnostic. A domain refusal is an explicit terminal result; coordinator unavailability
is not. Committed workspace effect receipts remain recoverable across coordinator failures.

Researcher report acceptance checks the current holder, lease epoch, assignment, and originating
execution. New reports from a terminal or superseded execution are refused. Accepted reports
retain their issuing-attempt authority after worker exit. Pending reports pin that attempt
against retries, resumes, and guidance-driven replacement until their disposition is recorded.

Domain effects and command results commit in one Coworld transaction. Assignment and release
effects instead commit with command-keyed receipts in the workspace store. Recovery reads those
receipts before reapplying an effect, including after a reservation has disappeared.

## Dispatch, outcomes, and recovery

The admission dispatcher delivers all persisted inputs through the same coordinator boundary.
Workflow advancement consumes outcomes and domain events. Independent reconciliation supplies
missed native facts through the same settlement function; it has no separate assignment policy.
In-process event notifications reduce latency. Durable event cursors and bounded cross-process
polling recover missed notifications.

Native admission freezes identity and replay conditions before transport. A known pre-send
failure does not establish native acceptance and can retry safely. Once acceptance is possible,
replay requires the same key, payload, authentication scope, runtime, and unexpired native
retention horizon. A changed scope or expired horizon never authorizes a fresh blind replay.
Terminal attempt status is absorbing. A delayed matching receipt may attach evidence but cannot
revive the attempt.

Live follow-ups steer the recorded run. A definite delivery rejection can retry; an ambiguous
reply retains the known target and is not resent. If the target ends before delivery, a durable
successor reroutes guidance without replaying the originating event. Source-message IDs stay
reserved, so another continuation cannot copy them.

Unplanned execution failures spend `recovery.max_attempts`. Successful waits, planned sleep,
and nonfailure reroutes do not. Exhaustion records an ordinary failed task for model review.
Definitively rejected guidance has its own count under the same configured limit. Transient
delivery refusals wait with backoff rather than spinning or creating unbounded successors.
Terminal rejection exhaustion records a visible delivery disposition and retains message
reservations. Guidance without source-message IDs follows the same durable retry policy.

Inference exhaustion is an intentional wait. It retains the queued identity without consuming
failure attempts. A spent-key wait ends when the normal lease owner installs a different key;
provider backoff can end at its declared deadline.

A chat message, and a follow-up admitted into a chat (origin `followup`: `xp.request` results
or a wake's `chat_update` updates), are the only admissions that can outlive a run. When its run fails on the budget, a
rate limit or a provider outage (`run_failures.py`), the same `op_id` waits and resumes as a new
run with a new native key and a continuation prompt, because the client follows the admission.
This is a new execution, not a replay of the old one. The hold is the only change that may clear
a recorded `run_id` (migration `0008_admission_waiting.sql`); any other change to a receipt
aborts. A late report of the failed run does nothing. Worker attempts still retry as successors.

## Release, pause, and sleep

A release request retains ownership while native acceptance or termination is unresolved.
The current attempt and every outstanding obligation for the task must be accounted for before
domain release and reservation removal. This includes a start accepted before its receipt arrives.
Stop acknowledgment is not termination evidence. A known worker that is still stopping remains
owned; a slow stop alone does not justify process-namespace exclusion. Committed terminal evidence
can finish release even after the native receipt has been retired.

An unknown execution or an unreplayable admission retains capacity and sleep obligations. Hosted
recovery asks the existing image supervisor to exit. Only replacement of that private runtime
namespace clears its uncertain execution obligation. There is no operator-only resolution state
or age-based ownership takeover in the managed path.

Unmanaged local servers do not own the gateway lifecycle. Their owner first stops the gateway
and workers, then uses `research workspace recover --runtime-stopped` to acknowledge exclusion.
That command refuses managed-runtime obligations and never replays uncertain input.

Pause is the common final gate for chat, assignments, retries, and model wakes. Domain command
application and outcome settlement can continue while paused. Routine `release_requested` is a
coordinator-driven transition, not a blocked execution requiring an operator.

`research workspace sleep` pauses admission, drains tracked runs and background processes,
settles recorded work, and flushes retained state. Unsettled execution or failed flushes prevent
readiness. Wake clears sleep-owned pause and the sleep marker before normal dispatch resumes.
This preserves the existing sleep lifecycle; it does not require an upstream runtime integration.

## Verification and limits

The [executable model](../../core/tests/test_execution_model.py) explores bounded acceptance,
receipt, outcome, pause, crash, release, and successor orderings. Its invariants require execution
exclusion, absorbing terminal outcomes, finite failure budgets, and durable handoffs. Recovery
requires eventual storage/service availability, observation, and supervisor progress.

[SQLite refinement tests](../../core/tests/test_transition_contract.py) cover commit and
notification cuts, receipt-after-outcome, exact successor transfer, release before a start
receipt, lost replies, cross-store effect acknowledgment, accepted reports after worker exit,
explicit lease epochs, bounded rejected guidance, and non-replay of ambiguous steering.

The [packaged transition probe](../../image/system_tests/check-transitions.py) runs in the pinned
native interpreter against the real plugin, inbox, and image coordinator. It checks awake,
unpaused status before proposing work, stable replay, changed-payload refusal, deliberate pause,
human approval, and command owners matching `/status.coordinator_instance`.

Its worker-report case uses an owned queued-attempt/session/lease fixture under the normal pause
gate. The installed callback submits a wait; the coordinator records its issuing attempt, epoch,
and result. A stale epoch is refused. No model or actual native worker is started for this case.
This proves the packaged report contract with fixture authority, not native worker execution,
post-exit acceptance ordering, or exclusion of an accepted worker.

The [restart probe](../../image/system_tests/check-recovery.sh) injects an execution obligation.
It checks retained bookkeeping, one supervisor/container restart, runtime identity replacement,
and recovery telemetry. It does not launch a native worker before losing its acceptance reply.
The combined packaged cut—actual worker acceptance, lost receipt, then verified exclusion by
container exit—remains unverified. Component and process-boundary evidence do not replace it.

Native descendant containment and lossless delivery of background results remain runtime limits.
The coordinator does not claim arbitrary shell or remote effects are reversible. No upstream
change, private runtime patch, or alternate credential path supplies missing guarantees.

## Merge and deployment sequencing

Done: the stack landed as one PR, #146, on November 3, 2026 (Pacific). The landing instruction
below is kept as written. The release and qualification steps after it describe the release
path; the Metta side of that path was not re-checked here.

Merge the complete reviewed stack through one landing PR targeting `main`. Preserve the reviewed
layer commits and update `main` once; do not merge review layers sequentially. Relevant `main`
pushes trigger [the distribution release workflow](../../.github/workflows/release-hermes.yml),
which dispatches Metta's image build. Image publication, hosted qualification, promotion, and
workspace adoption are separate milestones.

The hosted qualification worker owns promotion. It rejects a superseded candidate and conditionally
updates the qualified release pointer. That guard does not mean intermediate sequential merges
cannot deploy before a later candidate exists. The final stack tip is the intended release unit.
Deployed readiness requires qualification, promotion, and the affected normal user flow on the
target runtime; green component or packaged tests alone do not establish those milestones.
