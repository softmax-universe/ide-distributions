# Stale tool output and request latency

Measured November 9, 2026, against distribution main `3122d66e` and Hermes image
`sha256:d27689bc6e8581ec884a7227c9101fdaebf6c853cff8859020dac14f21452b4d`.

## Cause and change

An existing long chat retained 653 messages. Its text included 451,364 characters of tool output,
108,087 of assistant reasoning, 64,425 of tool arguments, 69,266 of assistant prose, and 26,804
of user text. These are character counts, not provider token counts or an exact reconstruction
of the earlier 300k-token requests.

The profile already enabled Hermes's deterministic pruning above 64,000 tokens. However, none of
the older results exceeded its default 8,000-character cutoff. Intermediate-sized file reads,
task records and platform responses accumulated below that cutoff. Model-window-based compaction
had not bounded this chat at 300k tokens.

Set the supported `compression.proactive_prune_min_result_chars` to 2,000 for both profiles,
including retained profiles through preparation. Keep the existing trigger, protected tail,
minimum reclaim and regrowth limits. Hermes archives originals and retains recovery references;
this adds no summary call, middleware, queue or background worker.

## Controlled latency experiment

Replayed the retained text history with reconstructed SOUL and all shipped tool schemas on
`moonshotai/kimi-k3.1-pro`, pinned to Moonshot through OpenRouter. One interrupted historical tool
round had no results and was omitted identically from both arms. Multimodal content was reduced
to its text in both. This is a controlled request replay, not the original wire request or a
deployed browser latency measurement. No production state was changed.

The original setting pruned zero results; the changed setting pruned 65 in 3.7 ms. Provider-reported
request size fell from **274,409 to 187,633 input tokens: 31.6% fewer**.

| Sample | Before seconds | After seconds | Before cached tokens | After cached tokens |
|---|---:|---:|---:|---:|
| 1 | 16.43 | 13.72 | 19,968 | 21,248 |
| 2 | 5.41 | 4.04 | 274,176 | 187,392 |
| 3 | 3.67 | 4.28 | 274,176 | 187,392 |

Median complete-response latency was 5.41 → 4.28 seconds. All six answers correctly identified
the latest candidate and that it had not been submitted. The sample is small, cache fractions
differ, and warm ranges overlap. Claim the request-size reduction; these measurements do not
establish a stable production percentile improvement.

## Verification and rejected alternative

The real-Hermes preparation test reads several results below 8,000 characters during one turn.
It checks that later outbound requests omit their full text, small results stay, and original
text survives in archived rows. Configuration tests cover new and retained profiles.

An experimental 128,000-token compaction trigger reduced the reconstructed history further but
took **164.97 seconds** for its summary. That trigger is excluded from this change. Aggressive
compaction needs its own recall, latency and amortization evaluation.

The remaining context includes protected recent results, user messages, reasoning, arguments and
schemas. This change is not a hard request-size cap. Production rollout and deployed E2E latency
remain separate from this PR's local and controlled-replay evidence.
