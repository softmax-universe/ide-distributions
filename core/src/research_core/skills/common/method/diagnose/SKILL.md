---
name: diagnose
description: Use when turning a weakness into testable hypotheses.
---

# Diagnose

Diagnosis turns a signal with evidence into two to four independent, testable hypotheses, each ready to become an Experiment. Run it after `survey` has decomposed a weakness or `replay-diagnosis` has named a cause.

## Method

1. **Check the record first.** `claim.search {query: <lever or symptom>, include_retired: true}`. A refuted claim closes its lever until new evidence meets the "what would change our mind" in its body: say which evidence does, or drop the idea. `experiment.search {query}` shows earlier attempts and their verdicts.
2. **Field or us?** A drop in rank with no change of ours is a scouting question (`scouting`), not a policy question. Read `platform.standings {league}` and the latest standings claims before blaming the policy.
3. **Explain the signal in game terms** with references: "the metric says X; in the episodes that is Y; here is the tick where it goes wrong" (`replay` and `measurement` evidence). The game's failure vocabulary and diagnostic instruments are game-specific: see `code/mixin/skills/<coworld>-diagnosis/SKILL.md` or the mixin docs.
4. **Propose two to four varied hypotheses.** Consider stop (a bad behaviour fires), enable (a good behaviour is absent) and amplify (a working behaviour is unreliable). If refuting one would refute them all, you wrote one hypothesis several times. Each has a mechanism ("X happens because Y, which causes Z"), not a tweak; a lever (the component that drives it, as a tag); a prediction that differs from what happens if it is false, naming the metric that moves; and the evidence it rests on.
5. **Record them as proposed Experiments.** `experiment_create {summary, goal, hypothesis {statement, if_true, if_false, motivation}, methods, tags: [<lever>, G-n], related_claims}`. A proposed Experiment is the hypothesis record; it stays editable until a Task starts it. Leave `methods.candidate` empty.
6. **Record the diagnosis as claims** (`writing-claims`): the cause, its evidence and how sure you are. A cause you could not see is a gap, not a zero.
7. **Propose the work.** `task_propose {type: improvement, goal, title, description, reasoning, experiment: X-n, for_human}` for the best one or two, with why this order and which levers you skipped as refuted. Do not start or run the Experiment in a research Task; the Orchestrator decides what runs.

## Failure handling

- The cause is not visible in the evidence: propose instrumentation (a maintenance Task that adds the missing logging or a game tool), not a behaviour change.
- Every idea matches a refuted lever: record that as a claim and propose scouting or a research Task on another aspect of the Goal.
- The signal is noise: a swing within the noise record (`calibrate-noise`) needs no hypothesis; say so.
