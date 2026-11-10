---
name: expert-consultation
description: Use when a hard question needs one concrete expert opinion.
---

# Expert consultation

Ask an advisory model one concrete question when progress is stuck: a hard policy design choice, ambiguous evidence, a repair that failed twice. Routine work needs no consultation. A recommendation is advice, never evidence: nothing is established until you verified it against the source and the platform.

## When

- Two attempts at the same diagnosis or repair failed and you can state what you tried.
- Two readings of the evidence are both plausible and you can say what would separate them.
- A design decision has a large cost (a verdict-sized Experiment) and a known body of practice you may be missing.

Not when the answer is in the mixin's docs, the Coworld's source (`coworld-source`), the forum or the claims (`claim.search`).

## How

1. Find the advisory tool Hermes gives this Session (a delegation or model tool); its absence is not a reason to stop ordinary work. Use the default model first; another only for a question the first answer did not resolve.
2. Ask it to act as a policy-research consultant on one question. Give it: the question; the relevant source, verbatim; the verified game and host contracts (`MIXIN.md`, the lab `AGENTS.md`, the manifest from `platform.coworld`); the original evidence (numbers with n, the decoded replay facts); the constraints; the approaches that failed and how. Exclude credentials and anything from the profile's environment.
3. Ask for a concise diagnosis or recommendation, a focused patch when relevant, and verification steps; ask it to separate observed facts from hypotheses and to name the evidence it is missing. For code, ask for plain source without line-number prefixes.
4. Do not ask it to start a research loop of its own, to consult another expert or to act on the platform.

## After

- Read the answer as advice. Inspect any proposed change against the actual game and policy source, then run the mixin's `check` and the relevant tests yourself. A consultation cannot establish that code compiled, uploaded, submitted or played.
- Continue the original Task yourself; keep the advice and what you verified. Record what the check established as a claim (`writing-claims`) with the real evidence (`code`, `measurement`, `experiment`); the consultation itself is at most a `transcript` link.
- Do not repeat the same consultation without new evidence; a second opinion is a different question or a different model.
- Game-specific advice is checked against the game: see `code/mixin/skills/<coworld>-diagnosis/SKILL.md` or the mixin docs before you accept a mechanism it names.
