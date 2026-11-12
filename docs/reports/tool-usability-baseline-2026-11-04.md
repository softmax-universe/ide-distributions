# Tool and skill usability baseline, November 4, 2026

How easy the research tools and skills are to find, read and use for Kimi K3.1 Pro, measured
before other branches change them. Measured at commit `f6a1e11c`. No tool or skill was changed.

**Replaced by [`tool-usability-2026-11-05.md`](tool-usability-2026-11-05.md).** Kept for its method
and how to read a run. Its findings and file references describe the code at `f6a1e11c`, and were
not updated as that code was fixed.

**2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

**Do not compare these numbers with a later run.** In this run every read got one placeholder line.
Later runs answer each read in the shape the real tool gives (`usability_reads.py`).

## The short version

- **Finding is mostly fine.** The model reached the right tool and action in 831 of 880 runs, and
  no skill or tool went unused. Most of the 49 misses are Researcher runs, and many of those come
  from a weakness in the test (see "Limits"), not from the tools.
- **First-try validity is the clearest problem, and it is cheap to fix.** 786 of 813 acting calls
  were valid at the first try. Most refusals trace to a few lines of our own text that show a call
  without one of its required fields, or to one habit. Findings 1 to 3 cover 23 of the 36 refusals.
- **Reading is not the problem.** Asked about each skill or tool alone, the model answered 690 of
  690 questions correctly. These are look-up questions with the text in front of it, so this shows
  no failure of reading and little more. The failures are about what it does in a session, so a
  before/after test of wording needs the findability part, not the reading part.
- **More skill reading is not an obvious goal.** Eleven situations where a skill was wanted had it
  opened in 0 of 5 runs and still went right 5 of 5. In the plain situations, opening an accepted
  skill made no measurable difference to the outcome (369/385 with, 98/105 without, p = 0.30). In
  the hard ones it went with better outcomes (122/136 against 32/44, p = 0.012), but that is a
  correlation in runs that chose to open it, not proof that opening it helped.

## What was measured

`core/tests/model/test_usability.py` and its `usability_*.py` helpers. Each run gets the real built
system prompt for the role (the SOUL, Hermes 0.22.6's own sections, the skill index, the briefing
and session context plugin sections) and the role's real tool list. The skill index was checked
byte for byte against what the pinned Hermes builds for both roles. Skills are exposed as they are
in a session: an index in the prompt and `skill_view` to open one.

| Part | Sample | Scored by |
|---|---|---|
| Findability and usability | 176 situations (137 plain, 39 hard; 120 Orchestrator, 56 Researcher), 5 repeats, 880 runs, 1,968 requests | The first call, the call that settled the run, skills opened, the tool's own argument check, and the argument rule of the situation (127 runs) |
| Comprehension | 46 items (28 skills, 17 tools, plus the Researcher's `task`), 3 questions each, 5 repeats, 690 answers | Each answer matched against patterns written from the source |
| Self report | The same 46 items, 3 repeats, 138 reviews | The model's own rating. Weighted low, and each complaint was checked against the code |
| Size | Every skill and tool | Bytes, and real tokens from the difference in OpenRouter's `prompt_tokens` |

Model `moonshotai/kimi-k3.1-pro`, pinned to Moonshot AI, which served every request. No named
`tool_choice`, no `temperature`. A run ends after at most 8 requests. Reads and other preparation
are answered and the run goes on. Any other write that was not wanted ends the run as `wrong_act`,
because production would have acted on it.

**Sample sizes.** Five repeats separate only large differences. 0/5 against 5/5 is p = 0.008; 3/5
against 5/5 is p = 0.44. **A single situation at 3/5 or 4/5 is not evidence by itself.** The
findings below rest on 0/5 or 1/5 results, on a cause seen in the code and in the refused calls, or
on totals across situations.

**Completeness.** Every cell is full: all 176 situations have 5 counted runs, all 46 items have 15
answers and 3 reviews, and all 46 sizes have a token count. No saved run has a host error or an
OpenRouter 402, which the test would have left out of every rate as `host_error`. The number 402 in
the saved runs' calls is only the model quoting the fixture's XP credits refusal, in `r-wait-credits`
and `r-hard-wait-credits-args`. No paid rerun followed the last counted run, because the key ran out; nothing here depends on one.

## Limits

1. **Every read returned the same placeholder.** In this run a read, `terminal` or similar call got
   one fixed line ("As the request describes; nothing further to add."). So an Experiment or Task
   the model looked up never showed any state. The model noticed: `task.complete` runs carry
   operation ids like `fail-t-21-tool-stubs`, and 6 runs mention a stub, one saying "All record tools (experiment.read, xp.status, task.read) return the same stub response".
   This hurt long Researcher runs most. Results marked "weak test" below should not be read as tool
   failures. Later code answers each read with `usability_reads.py`; the baseline was not rerun.
2. **The world is a fixture.** One briefing, one Goal, one Coworld (`cogs`), scripted refusals.
3. **The plain tier is near its ceiling** (662/685), so it can show a regression better than an
   improvement. The hard tier (169/195) is where gains will show.
4. **Some runs were redone.** A first full run exposed test errors (missing real `next` hints,
   Hermes's `category:name` skill names, some fixture rules). Those were fixed in `f6a1e11c` and
   the 148 affected runs repeated. The counts therefore mix two sessions on the same code, with the
   same tools and skills.
5. **Researcher's real default model differs.** `researcher/config.yaml` defaults to
   `recursive/flint-v1.0:free`; this test uses Kimi, as the task asked, and Kimi gets the
   Kimi-gated "Execution discipline" prompt section.
6. **Self reports have 3 repeats**, against 5 for everything else.
7. **Two mocks were wrong in this run.** The `xp` credits refusal fixture lacked the real `next` text
   from `core/src/research_core/tools/xp.py` (`_credits_refused`), which tells a Researcher to call
   `task.wait`, so `r-hard-wait-credits-args` at 3/5 is unfairly low. The `task.claim` mock returned
   `task` and `messages`, where the real result has `record`, `worktree`, `branch`, `instructions`
   and `pending_messages`. Reads of the Task or Goal right after a claim were common, and should not
   be taken as a fault. Both are fixed in later code, not measured.
8. **Judging and fixtures changed after this run**, so the rerun may move these numbers:
   - A call the checks refuse gets its refusal and the run goes on. Here it could end as `wrong_act`:
     one of the 30 is that case (`r-candidate`, `experiment.create` with an empty `methods.candidate`).
   - A store check's refusal is answered as a refusal. Here the model was told it had worked.
   - A write later in the reply that settled a run right makes it `wrong_act`. Here later calls in
     that reply went unseen, and the raw runs did not keep them, so how many there were is unknown.
   - `delegate_task` counts only when it spawns a task. All 8 here did.
   - X-5 in `r-candidate`, `r-wait-build`, `r-hard-wait-build-args` and `r-hard-resume-build` now reads
     as started, as the Task's own instructions have it, and each situation's records agree with its
     text (`o-hard-explicit-verdict`'s renamed metric, `o-hard-review-reopen`'s floor of 100,
     `o-hard-goal-complete`'s rank 3, the deliverables of a Task under review).
   - In `o-hard-after-xp-stop` and `r-hard-end-after-wait` any call but opening a skill now counts as
     not stopping. Here one `o-hard-after-xp-stop` run polled `xp.status` first and scored right.
   - An empty reply is no longer a right answer. None occurred here.
   - `o-hard-merge-decision`, `o-hard-need-from-user` and `o-submission-ask` now declare client
     capabilities. Here their session context said "ask in chat when only the user can answer", while
     they wanted `ui.ask` or a panel, which may explain part of `o-hard-need-from-user`'s 3/5. A real
     wake session with no declared capabilities would read the same; that is not checked here.
   - A Version this run uploaded reads as uploaded afterwards, and `task.complete`'s deliverables meet
     the argument rule under `checklist` as well as flat.
   - Argument rules are stricter on value and looser on order: ids match whole, the value of
     `o-hard-setting-not-directive` and the arms of `r-hard-xp-field-args` are exact, and lists may come
     in any order. No counted run here changes under them, so 124/127 stands.
   - `r-hard-ideation-no-ideation`'s Task sits under G-2, as its text says. V-12 reads as uploaded where
     an evaluation has played it, and `upload` is allowed before `task.propose_submission` in
     `r-propose-submission`, whose 1 `upload` ending below would no longer be a miss.
   - `r-ask`'s Task sits under G-2, because G-1 names the league it says no record settles, and
     `settings.read` in `o-submission-ask` now shows mode `ask`. Both were 5/5 here.
   - `delegate_task`'s text matches what a real session froze, one sentence shorter.
9. **Commands are not judged.** `terminal` and `execute_code` count as preparation whatever they run,
   so a write through them would pass as a read. The raw runs did not keep their arguments. The
   forum injection result below is not affected: those 5 runs made no call at all.
10. **Refusals inside a tool's own code are not reproduced.** The test runs the argument check and,
   for four actions, the store's checks. A rule such as "standings needs a division or a league",
   which the tool's code enforces, gets no refusal here.

## Headline numbers

| Scope | Runs | Right | Right call first | Valid at first try | Arguments as the rule says | Accepted skill opened | Mean calls before acting |
|---|---:|---:|---:|---:|---:|---:|---:|
| All | 880 | 831 | 435 | 786/813 | 124/127 | 521/670 | 3.89 |
| Orchestrator | 600 | 588 | 387 | 552/573 | 64/64 | 347/420 | 2.28 |
| Researcher | 280 | 243 | 48 | 234/240 | 60/63 | 174/250 | 7.34 |
| Plain | 685 | 662 | | 649/663 | | 385/490 | |
| Hard | 195 | 169 | | 137/150 | 124/127 | 136/180 | |

Outcomes: right 831, `wrong_act` 30 (one of them a call production refuses: Limits, item 8), words
where a call was wanted 9, stopped at the request limit 10. "Right call first" is low for the Researcher because its Task text tells it to load skills and
read first, which is what it should do, so that column is not a failure rate for that role.

The prompt before any call is 22,551 tokens for the Orchestrator and 21,107 for the Researcher. The
skill index takes 2,038 and 1,580 bytes of that. A Researcher run reads a mean of 2,055 tokens of
skills (highest 5,950), an Orchestrator run 777 (highest 3,526).

## Findings: behaviour measured

Ranked by how much each costs against the goal that any tool is called correctly first try. Short
paths here and below are under `core/src/research_core/`, and a skill's `<name>/SKILL.md` is under
`skills/common/<group>/` or `skills/<role>/`.

### 1. `task.review` is refused for lack of `operation_id` in 14 of 20 first tries

- **Files:** `core/src/research_core/skills/orchestrator/review/SKILL.md:31`,
  `core/src/research_core/watcher/wakes/review.md:20`.
- **Evidence:** valid at first try 6/20 (`o-review` 0/5, `o-hard-review-candidate` 3/5,
  `o-hard-review-reopen` 2/5, `o-hard-review-refuted` 1/5). 14 of the 36 refusals in the whole run,
  all this one field. `review` was opened 20/20. Every run recovered on the next call and ended
  right, so the cost is one wasted request each.
- **Cause:** both places write the call as `task.review {task, accept, merge, reason}` and
  `{accept, merge, reason}`. Seen in the refused calls: they send exactly those fields. That the
  text is why is an inference, though the sent fields match it closely.
- **Fix:** write `operation_id` into both, or stop requiring it if another branch makes it
  generated. The same omission is in `task.claim` (`wakes/task_start.md:3`, `task/SKILL.md:12`,
  `working-a-task/SKILL.md:13`), `task.create` (`goals-and-tasks/SKILL.md:33`,
  `merge-conflicts/SKILL.md:41`) and `workspace.change_complete` (`merge-conflicts/SKILL.md:34`).
  No refusal was measured for those, which shows the text alone does not always mislead, but they
  should be fixed together.

### 2. A background build's `next` hint shows a `task.wait` that gets refused, and offers the wrong call first

- **File:** `core/src/research_core/tools/version.py:326-330`.
- **Evidence:** the hint says `task.wait {kind: process, ref: <pid>}`, but `task.wait` requires
  `detail` (`tools/task.py:483`). `r-wait-build` was valid at first try 3/5, with 4 refusals for
  `detail`, on runs that opened no skill. Over `r-wait-build` and `r-hard-wait-build-args`, 6 of 10
  runs waited and 4 called `version.create` while the build was still running: 2 at once with the
  resume token, 1 at once without it, which would start the build again, and 1 with the token after
  three refused waits. The hint offers resume first; `candidate/SKILL.md:29` says a Researcher must
  wait, but none of these 10 runs had it open.
- **Fix:** add `detail` to the hint, and make it depend on the role as `_credits_refused` in
  `tools/xp.py` does: a Researcher gets only the wait. The hints at `tools/xp.py:394`, `:523` and
  `:613` have the same form without `detail`. No refusal was measured from those, but they should
  match.

### 3. `experiment.update_run` is refused 5 of 5 times for sending `experiment`

- **File:** `core/src/research_core/tools/experiment.py:568-577`.
- **Evidence:** `o-experiment-update-run` valid at first try 0/5, all "experiment is not a field of
  update_run". All recovered. The tool is right that the run id is enough. That the model adds
  `experiment` out of habit from the other actions on an Experiment is an inference.
- **Fix:** accept and ignore a matching `experiment` on `update_run` (best, since it costs the model
  nothing), or say in the action's text "the run id alone names it; do not send `experiment`".

### 4. A session's opening goal is ignored: 0 of 5 create a Goal

- **Files:** `core/src/research_core/plugin/session_context.py:31-33`, also
  `core/src/research_core/plugin/turn.py:77`,
  `core/src/research_core/skills/orchestrator/client-web-ide/SKILL.md:53`.
- **Evidence:** `o-hard-opening-goal`, where the user says "Hello!" and the session context declared
  at open carries `opening_goal`. 5/5 replied in words, asking what to work on. `client-web-ide` was opened 2/5,
  and neither of those runs acted on it. The only explanation of the field is line 53 of that
  skill, so the context shows the value with no word of what it means.
- **Fix:** label the value where it is rendered, in `session_context.py` (the path this situation
  uses) and in `turn.py`, for example "what the user typed when opening this session; treat it as
  their first request". This is one fixture with a greeting
  as the first message, the hardest form of this case.

### 5. A Researcher told to stop fails the Task instead of releasing it

- **File:** `core/src/research_core/tools/task.py:521-522`,
  `core/src/research_core/skills/researcher/working-a-task/SKILL.md:73`.
- **Evidence:** `r-release` 2/5. The 3 misses called `task.complete {outcome: failed}` at once with
  no skill opened; the 2 right runs opened `working-a-task`. The action's own text ("Request release;
  ownership stays reserved until execution is proven stopped") says what it does, not when to use it.
- **Fix:** put the rule in the action text: "Use when you must not continue, such as a directive
  forbidding the only path; use `complete` with `failed` when you tried and it did not work."

### 6. Comparing two Versions leads to `survey`, not `experiment`

- **Files:** `skills/common/method/survey/SKILL.md` and `experiment/SKILL.md` (their index
  descriptions).
- **Evidence:** `o-hard-better-not-survey` ("Is V-12 better than V-11? Look at their recent episodes")
  2/5, `survey` opened 5/5, `experiment` 0/5, even though it is the right way to answer. The situation
  accepts either skill, so the skill counts as found 5/5; the miss is what the model did next, and
  the 2 right runs ended on `experiment.search`, a read that decides nothing. Partly a weak test: 2 of
  the 3 misses gave up because the reads returned nothing.
- **Fix:** add to `experiment`'s description, for example "Use when deciding whether one Version beats
  another, and when designing, running and closing an Experiment", and to `survey`'s "never decides
  which Version is better".

### 7. `xp.request` for a comparison often leaves the opponents unpinned

- **Files:** `core/src/research_core/tools/xp.py:665`,
  `core/src/research_core/skills/common/method/run-eval/SKILL.md:14`,
  `core/src/research_core/task_types/improvement.md:42-45`.
- **Evidence:** `r-hard-xp-ab-args`, 2/5 sent the pinned roster; `run-eval`, which holds the rule, was
  opened 1/5. The call is valid either way, so nothing refuses it, and the skill says a comparison
  needs identical pinned rosters. Five runs is few, but the rule is simply not on the path: the
  improvement Task's load list omits `run-eval`.
- **Fix:** add `run-eval` to that load list, or better put the rule into the `opponents` description:
  "Pin these for a comparison; both arms then meet the same roster."

### 8. Game binding skills are named as skills, and opening them fails

- **Files:** every "see the mixin's `<coworld>-ab` skill or docs" line: `experiment/SKILL.md:22`,
  `run-eval/SKILL.md:16,19`, `calibrate-noise/SKILL.md:16`, `analyze-opponents/SKILL.md:18`,
  `survey/SKILL.md:14`, `diagnose/SKILL.md:14`, `replay-diagnosis/SKILL.md:19`,
  `proposing/SKILL.md:33`, `review/SKILL.md:22`, and the task types.
- **Evidence:** 11 failed `skill_view` calls for `cogs-*` names in 880 runs (`cogs-eval-design` 4,
  `cogs-ab` 3, `cogs-replay-inspection` 2, `cogs-diagnosis` 1, `cogs-survey` 1). These are read by
  path from the session context; Hermes 0.22.6 does not look in the place they live, and they are
  not in the index. Each costs one request.
- **Fix:** write "read the mixin's `<coworld>-ab` skill file (session context lists the path)". Check
  this one against a live session first, because the session context here comes from a fixture.

### 9. The `task` skill is close to dead

- **File:** `core/src/research_core/skills/common/records/task/SKILL.md`.
- **Evidence:** opened in 8 of 880 runs, against 117 for `working-a-task`, which covers the same
  ground for the Researcher. It is 4,077 bytes and takes an index line for both roles, and its
  description ("working a Task from claim to completion") does not fit an Orchestrator, which
  cannot claim Tasks (`tools/task.py:462-467`). No situation had it as the first choice, so this measures indifference, not
  failure.
- **Fix:** remove it from the index and fold whatever it alone says into `working-a-task` and
  `goals-and-tasks`. Saves about 1,043 tokens when opened and one index line always.

### 10. Researcher Task text loads three skills before the model knows what it needs

- **File:** `core/src/research_core/task_types/improvement.md:42-45`, also
  `task_types/research.md:23`.
- **Evidence:** `writing-claims` opened 186 times in 880 runs, 146 of them in situations that did not
  list it; `candidate` 66 times, 56 of them; Researcher means of 7.34 calls before acting and 2.25 skills
  per run, against 2.28 and 0.82. The placeholder reads (Limits, item 1) inflate those long runs, so
  the size of the effect is unknown; the loading itself is what the text instructs.
- **Fix:** tie each skill to the step that needs it: "load `candidate` before building, `experiment`
  before designing, `writing-claims` before the first claim".

### Weak test: do not act on these without realistic reads

These are low, but the runs show the placeholder in play (operation ids naming stubs, repeated
reads, `experiment.create` for an Experiment the model could not see). Same for 10 `limit` outcomes.

| Situation | Right | What happened |
|---|---:|---|
| `r-candidate` | 0/5 | limit 2, `experiment.create` 2 (one refused: Limits, item 8), `task.complete` 1 |
| `r-run-eval-field` | 1/5 | `experiment.create` 2, `task.complete` 2 |
| `r-experiment-start` | 2/5 | limit 2, `experiment.update` 1 |
| `r-hard-xp-field-args` | 2/5 | `experiment.create` 2, `task.complete` 1 |
| `r-experiment-create` | 3/5 | limit 2, all with 4 skills read |
| `r-expert` | 3/5 | `task.complete` 2 |
| `r-hard-wait-credits-args` | 3/5 | Also a wrong mock (Limits, item 7) |

### Not failures

- Skill wanted, opened 0/5, right 5/5, so the tool list is enough: `o-task-message`,
  `o-task-workers`, `o-coworld-switch`, `o-platform-replay`, `o-platform-champion`,
  `o-schema-newer`, `o-hard-after-xp-stop`, `r-start`, `r-wait-xp`, `r-propose-followup`,
  `r-hard-resume-build`. In the last, 3 runs were refused once for `summary` first (table below).
- Hard cases that held: a prompt injection in a forum post was resisted 5/5, a plain greeting was
  answered in words 5/5, and the model stopped after an XP request 5/5, one of those after polling
  `xp.status` once (Limits, item 8). `o-hard-setting-not-directive`
  went right, with the right arguments.

## Findings: wording faults checked against the code

These began as the model's self report or from reading the text. They are true of the code, but no
measured behaviour failed because of them, so they rank below everything above. "(3/3)" is how many
of the 3 self reports raised it.

| File | Fault | Checked how | Fix |
|---|---|---|---|
| `tools/settings.py:105` | `value`: "null clears a nullable setting", but the schema's `anyOf` leaves out null (3/3) | Schema; `validate_args` skips None | Allow null in the schema |
| `tools/experiment.py:96` | `decision_rule.k`: "stored, unused by the rule" (3/3 would cut it) | `experiment.py:380`: the power check uses it, the rule does not | Say what it is for |
| `tools/submit.py:247`, `submission/SKILL.md:38` | `lineage` is never defined (3/3) | Only behaviour is in `platform/fake.py:630`: champion if the policy name already is | Define it, after checking the real platform |
| `tools/platform.py:72,259`, `tools/ui.py:105`, `tools/version.py:392`, `tools/xp.py:417` | "One of these" rules sit behind fields all marked optional | Lines read | State the rule in the action's text |
| `store/tasks.py:878`, `review/SKILL.md` | `merge` is required even with `accept: false`; the skill is silent. Models sent `merge: no` 5/5 in `o-hard-review-reopen`, so nothing failed | Code | One sentence in the skill |
| Researcher `task` schema | The nested `checklist` duplicates the flat fields (3/3); the handler merges both | `handle_complete` | Drop one form, saves schema tokens |
| `task/SKILL.md`, `task` schema | `failure.tried` is a string in the schema, a list in the skill's example; the validator accepts a list | Code | Match them |
| `writing-claims/SKILL.md:14` | "links do not change" sits beside `add_evidence` (3/3) | Text | Separate evidence from links |
| `review/SKILL.md:10` | "`ui.notify` is your only channel", yet `ui.ask` applies in mode ask (2/3) | Text | Correct it |
| `user-identity/SKILL.md:17` | "Nothing gates it" yet `ui.ask` if surprised (3/3) | Text | Correct it |
| `merge-conflicts/SKILL.md:20` | `git log main..task/<n>` and `git diff main...task/<n>` do not show what moved on `main` (1/3) | git's rules | Add `task/<n>..main` |
| `calibrate-noise/SKILL.md` | Promises guardrail tolerances and never derives them; `k` undefined (3/3) | Text | Derive them or drop the promise |
| `working-with-the-ide/SKILL.md:36-37` | The panel kind list omits `agent-context`, which the schema has and line 53 of the same skill uses (1/3) | Schema | Add it |
| `coworld-source/SKILL.md:16`, `survey/SKILL.md:20,30` | "source map" is never defined and "taint" only by example (3/3) | grep | Define them |
| `goals-and-tasks/SKILL.md`, `client-web-ide/SKILL.md` | "Retry unchanged after uncertainty" is too terse; "a client view the capabilities list" misreads (3/3 each) | Text | Reword |

Found by reading the text alone, also true, not raised by the model:

| File | Fault | Fix |
|---|---|---|
| `watcher/wakes/setup.md:14` | "with the setup skill": no such skill exists | Remove or add it |
| `mixin-from-scratch/SKILL.md:39` | Says `version.create` refuses an image artifact; `candidate` and `store/versions.py` (`ARTIFACT_KINDS`: file, image) say otherwise. Stale | Update |
| `goals-and-tasks/SKILL.md:31` | "Repairs ... the only Tasks that may have no Goal"; the store allows any maintenance Task. The model repeats this as fact in the comprehension test | Correct it |
| `candidate/SKILL.md:29-30` | The call is written `version.create {resume}`, without the required `summary`; only line 29 adds "with the same arguments". `r-hard-resume-build` valid at first try 2/5, 3 refusals for this, all recovered | Add it. This one has measured evidence but costs one retry |
| `core/tests/model/hermes_builtin_tools.json` | On `main` it holds Hermes 0.21.6's tool text; the pin is 0.22.6, with different `terminal` and `skill_view` descriptions | The unmerged `tools/model-test-gaps` branch refreshes it |

Other refusals, each 1 or few and without a clear cause in our text: `claim.search` with an empty or
missing `query` 4 and `experiment.search` with an empty one 1 (all in `r-propose-ideation`, listing
what exists), `platform.forum_list` with `league` 1,
`task.answer` and `task.propose` without `operation_id` 1 each, `experiment.search` with
`related_experiments` 1, `experiment.create` with an empty `methods.candidate` 1. That last one may
come from our text: `experiment/SKILL.md:21` and `diagnose/SKILL.md:16` say to leave it empty, and
it has to be left out.

## Self-report complaints not upheld

- `experiment.update_run` leaving out `experiment` was called a fault of the docs. The text is
  correct; finding 3 is about the model's habit, not an error.
- `repair`'s "4xx means shape changed" drew a complaint, but this is judgement, with nothing in the
  code to check it against.
- "succeeded", "accepted" and "approved" against the status enum's `complete` was raised as a
  contradiction. The store checks status `complete` and outcome `succeeded`, so it is only
  terminology.

## Per skill

"Wanted first": situations where this skill is the first choice. "Accepted": this one or another
acceptable skill opened. "Opened elsewhere": opened in situations that did not list it. Clarity is
the self report's mean of 1 to 5, over 3 reviews; comprehension is out of 15. Ordered from the lowest
accepted rate.

| Skill | Bytes | Tokens | Wanted first | Accepted opened | This skill opened | Opened elsewhere | Clarity | Comprehension |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `candidate` | 3415 | 788 | 5 | 10/25 | 10/25 | 56 | 4.0 | 15/15 |
| `replay-diagnosis` | 3323 | 768 | 2 | 5/10 | 5/10 | 27 | 4.33 | 15/15 |
| `credits` | 2596 | 596 | 6 | 15/30 | 12/30 | 22 | 4.33 | 15/15 |
| `working-with-the-ide` | 4361 | 1051 | 5 | 13/25 | 13/25 | 7 | 3.67 | 15/15 |
| `working-a-task` | 4475 | 1100 | 6 | 17/30 | 13/30 | 95 | 4.0 | 15/15 |
| `client-web-ide` | 3658 | 856 | 2 | 7/10 | 7/10 | 1 | 4.0 | 15/15 |
| `goals-and-tasks` | 4803 | 1158 | 23 | 84/115 | 83/115 | 17 | 4.0 | 15/15 |
| `coworld-source` | 3398 | 810 | 3 | 11/15 | 11/15 | 1 | 4.0 | 15/15 |
| `repair` | 4273 | 973 | 5 | 19/25 | 18/25 | 6 | 4.0 | 15/15 |
| `proposing` | 2982 | 706 | 5 | 19/25 | 19/25 | 9 | 4.67 | 15/15 |
| `experiment` | 4403 | 1044 | 19 | 75/95 | 69/95 | 45 | 4.0 | 15/15 |
| `diagnose` | 2871 | 654 | 1 | 4/5 | 4/5 | 9 | 4.0 | 15/15 |
| `environment` | 4320 | 960 | 3 | 12/15 | 10/15 | 23 | 4.0 | 15/15 |
| `run-eval` | 3261 | 767 | 6 | 25/30 | 16/30 | 13 | 4.33 | 15/15 |
| `scouting` | 3397 | 780 | 6 | 25/30 | 25/30 | 23 | 4.0 | 15/15 |
| `user-identity` | 2880 | 671 | 2 | 9/10 | 9/10 | 0 | 4.0 | 15/15 |
| `merge-conflicts` | 3139 | 776 | 3 | 14/15 | 14/15 | 0 | 4.0 | 15/15 |
| `softmax-api-routes` | 5441 | 1575 | 4 | 19/20 | 19/20 | 6 | 4.67 | 15/15 |
| `submission` | 3443 | 795 | 8 | 38/40 | 38/40 | 8 | 4.0 | 15/15 |
| `analyze-opponents` | 3604 | 820 | 2 | 10/10 | 10/10 | 1 | 4.0 | 15/15 |
| `calibrate-noise` | 2860 | 652 | 1 | 5/5 | 5/5 | 4 | 4.0 | 15/15 |
| `survey` | 3327 | 741 | 1 | 5/5 | 5/5 | 18 | 4.0 | 15/15 |
| `expert-consultation` | 2736 | 568 | 2 | 10/10 | 10/10 | 0 | 4.0 | 15/15 |
| `writing-claims` | 3743 | 949 | 8 | 40/40 | 40/40 | 146 | 4.0 | 15/15 |
| `client-coplay` | 3314 | 760 | 1 | 5/5 | 5/5 | 1 | 4.0 | 15/15 |
| `mixin-from-scratch` | 4437 | 1105 | 1 | 5/5 | 5/5 | 0 | 4.0 | 15/15 |
| `review` | 4450 | 1078 | 4 | 20/20 | 20/20 | 0 | 4.0 | 15/15 |
| `task` | 4077 | 1043 | 0 | - | - | 5 | 4.33 | 15/15 |

The skills total 24,544 tokens. Never opened: none, though `task` (8) and `client-coplay` (6) are
close.

**Read the low rows with care.** `candidate` is low mostly from `r-candidate` and its neighbours,
and the 0/5 situations in "Not failures" pull others down without any harm done. Rates below 5/5
here mean the model did without the skill, which is a failure only where the act also went wrong.

**Confusions.** When the wanted skill did not open, the model mostly opened nothing (the first
number below), rather than the wrong one.

| Wanted | Instead |
|---|---|
| `goals-and-tasks` (115) | nothing 26, `run-eval` 3, `task` 1, `experiment` 1 |
| `experiment` (95) | nothing 20 |
| `candidate` (25) | nothing 15 |
| `working-with-the-ide` (25) | nothing 10, `submission` 1, `goals-and-tasks` 1 |
| `working-a-task` (30) | nothing 10, `writing-claims` 3 |
| `credits` (30) | nothing 9, `candidate` 3, `working-a-task` 2, `writing-claims` 1 |
| `repair` (25) | nothing 6 |
| `scouting` (30) | nothing 5 |
| `proposing` (25) | nothing 4, `writing-claims` 1, `working-a-task` 1 |
| `coworld-source` (15) | nothing 4 |
| `replay-diagnosis` (10) | nothing 4, `cogs-replay-inspection` 1 |
| `client-web-ide` (10) | nothing 3 |
| `run-eval` (30) | `working-a-task` 2, `candidate` 2, `experiment` 1 |
| `environment` (15) | nothing 2, `working-a-task` 1 |
| `submission` (40) | `candidate` 1, `working-a-task` 1 |
| `merge-conflicts` (15) | `repair` 1 |
| `softmax-api-routes` (20) | `working-a-task` 1 |
| `user-identity` (10) | nothing 1 |
| `diagnose` (5) | nothing 1 |

Names that do not exist: the binding names in finding 8, and `method.survey` and
`method.replay-diagnosis` once each.

## Per tool

Sizes are alone, with no other tool beside them. Counts add up the situations whose first-listed act
is one of the tool's actions, so a situation that accepts several acts credits that tool even when
another of them ended the run. A tool can be right at a low "first" rate because the model reads first; that is not a fault.

| Tool | Bytes | Tokens | Right call reached | Right call first | Valid at first try | Clarity | Comprehension |
|---|---:|---:|---:|---:|---:|---:|---:|
| `xp` | 2149 | 385 | 32/40 | 2/40 | 32/32 | 3.67 | 15/15 |
| `version` | 2536 | 481 | 30/35 | 24/35 | 27/30 | 4.0 | 15/15 |
| `goal` | 2468 | 465 | 39/45 | 26/45 | 39/39 | 4.0 | 15/15 |
| `experiment` | 10171 | 1668 | 71/80 | 20/80 | 66/71 | 4.0 | 15/15 |
| `task` | 6960 | 1275 | 177/190 | 65/190 | 160/178 | 3.33 | 15/15 |
| `task` (Researcher's schema; counts above) | 9163 | 1611 | | | | 3.67 | 15/15 |
| `ui` | 2867 | 526 | 33/35 | 13/35 | 33/33 | 4.0 | 15/15 |
| `claim` | 4943 | 963 | 58/60 | 13/60 | 58/58 | 4.0 | 15/15 |
| `directive` | 1466 | 278 | 29/30 | 24/30 | 29/29 | 4.0 | 15/15 |
| `forum_post` | 1303 | 246 | 20/20 | 15/20 | 20/20 | 4.0 | 15/15 |
| `github` | 1071 | 207 | 10/10 | 10/10 | 10/10 | 4.67 | 15/15 |
| `platform` | 4858 | 933 | 165/165 | 106/165 | 164/165 | 4.0 | 15/15 |
| `settings` | 1266 | 222 | 25/25 | 17/25 | 25/25 | 3.33 | 15/15 |
| `softmax_api` | 1246 | 230 | 25/25 | 19/25 | 25/25 | 4.67 | 15/15 |
| `submit` | 1351 | 250 | 10/10 | 9/10 | 10/10 | 4.0 | 15/15 |
| `upload` | 842 | 161 | 20/20 | 10/20 | 20/20 | 5.0 | 15/15 |
| `wiki_edit` | 1242 | 230 | 15/15 | 10/15 | 15/15 | 4.0 | 15/15 |
| `workspace` | 2941 | 548 | 45/45 | 34/45 | 45/45 | 4.33 | 15/15 |

Never called: none.

**What ended the run when the wanted action did not:**

| Wanted | Right | Instead |
|---|---:|---|
| `task.release` | 2/5 | `task.complete` 3 |
| `goal.create` | 5/10 | words 5 |
| `version.create` | 10/15 | limit 2, `experiment.create` 2, `task.complete` 1 |
| `experiment.start` | 7/10 | limit 2, `experiment.update` 1 |
| `xp.request` | 22/30 | `experiment.create` 4, `task.complete` 3, limit 1 |
| `task.wait` | 22/30 | `version.create` 4, `task.complete` 3, `experiment.close` 1 |
| `experiment.create` | 20/25 | limit 3, words 2 |
| `delegate_task` | 8/10 | `task.complete` 2 |
| `task.propose_submission` | 8/10 | `upload` 1, limit 1 |
| `ui.panel_set` | 13/15 | words 1, `goal.update` 1 |
| `directive.add` | 9/10 | words 1 |
| `claim.write` | 18/20 | `task.complete` 1, limit 1 |
| `goal.set_status` | 14/15 | `task.abandon` 1 |
| `experiment.close` | 14/15 | `task.complete` 1 |
| Answer in words (`text`) | 19/20 | `experiment.close` 1 |

## Comparing a later run

```bash
OPENROUTER_API_KEY=... IDE_HERMES_SRC=/path/to/hermes-agent \
  uv run pytest -m model core/tests/model/test_usability.py -k measure
```

The run compares itself with `core/tests/model/usability_baseline.json` and prints the headline
counts, then only what passes Fisher's exact test (p < 0.01 for a situation, p < 0.05 for a whole
skill or tool, since about 180 situations would otherwise show differences by chance). Each situation
and text carries a hash, so a changed one is reported as changed and not compared, and whole-skill
and whole-tool totals are left out while any situation differs, because they would add up different
situations. `meta.reads` records which reads a run used, and when two runs differ in it the comparison
opens by saying they are not comparable as a change in the tools. Write a new baseline from such a
run with `USABILITY_WRITE_BASELINE=1`, leaving tools and skills as they are, then compare later changes
with it.

`USABILITY_SITUATIONS` runs a few situations and `USABILITY_PARTS` runs some parts; `USABILITY_RUN_DIR`
resumes a stopped run, and is refused if the tools, skills, situations or test changed since it
began (the default folder is one per day, so a second run that day needs a new one);
`USABILITY_WRITE_BASELINE=1` replaces the baseline. The headline counts are
meaningless on a narrowed run, because they only add up what ran. The per-situation lines still hold.

The file holds counts, hashes and sizes only. The raw runs were not committed, so a few figures here
rest on them and cannot be checked from the file: each refused call, the cross-tab of skill opening
and outcome, the tokens of skills read, and the costs of runs that were not kept.

## What it cost

The 880 kept runs cost $25.63 (about $0.15 a situation) and comprehension, self reports and sizes
$4.83, so this run's **full repeat is about $30.50**. Expect the next one to cost more, because real
reads are longer than one line. 96% of prompt tokens came from the cache. Wall time was not measured
cleanly, since the kept runs came from several resumed sessions. Reaching this baseline cost more:
the saved runs add up to about $47.50 (a partial run $11.12, the first full run with the errors in
"Limits" $25.53, the 148 redone runs $5.97, the other parts $4.83), and a pilot on top was not saved.
