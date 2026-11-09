# Where hosted turn time goes

Measured November 9, 2026, from the agent telemetry operation lines (`service:ide-agent-telemetry`,
every model attempt and tool call from 2026-11-07 02:35 to 2026-11-09 23:15 UTC: 309 turns, 3,761
main-loop model calls, 5,072 tool calls) and the sampled `ide-distributions` spans over seven days
(457 turns, 7,567 `chat` spans). Read-only; nothing in production changed.

## Many calls, then slow calls

Model time is 86% of the 124 turns over 40 seconds; tool time is 13%. Across those turns, the
number of model calls explains 61% of the variance of log model time, and the time per call 23%.
Spans over seven days give 51% and 24%.

| Model calls per turn | Turns | Over 40 s | Median turn | Share of all turn time |
|---:|---:|---:|---:|---:|
| 1 | 18 | 0 | 5 s | 0.2% |
| 2 | 3 | 0 | 8 s | 0.1% |
| 3 | 36 | 2 | 15 s | 1.4% |
| 4–5 | 100 | 9 | 22 s | 5.7% |
| 6–10 | 76 | 37 | 39 s | 10.2% |
| 11–20 | 23 | 23 | 172 s | 10.5% |
| 21–40 | 34 | 34 | 472 s | 35.5% |
| 41–80 | 16 | 16 | 686 s | 27.3% |
| 81+ | 3 | 3 | 1,081 s | 9.2% |

## Why a call is slow

| Model | Calls | Duration p50 / p90 | First token p50 / p90 | Input p50 | Uncached p50 | Output p50 | Output tokens/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| moonshotai/kimi-k3 | 1,856 | 10.2 / 31.5 s | 3.9 / 10.1 s | 41k | 1.1k | 244 | 53 |
| moonshotai/kimi-k3.1-pro | 1,233 | 4.3 / 8.5 s | 3.2 / 4.2 s | 36k | 1.8k | 226 | 172 |
| recursive/flint-v1.0:free | 516 | 4.9 / 18.5 s | 1.3 / 2.7 s | 111k | 1.3k | 426 | 140 |

Inputs and outputs are similar for the two Kimi models; Kimi K3 decodes at a third of the speed.
Of the 369 calls over the p90 (22.5 s), 340 spent most of their time generating, not waiting for
the first token, and 328 were Kimi K3. Their median output was 1,181 tokens, mostly authored
content: `write_file` (1,342 output tokens at the median, 31 s) and `claim_write` (897, 21 s).
The cache read 94.1% of input tokens; no attempt was a retry.

Every Researcher Session starts on the free Flint model, gets HTTP 429 from its daily quota, and
falls back to Kimi K3 for the rest of the Session (64 such fallbacks; 1,856 of the Researcher's
2,609 calls). Hence `migrate_superseded_model` and the shipped fallback change.

## What fills the context

The last request of 12 long Sessions: tool results 44.6%, replayed reasoning 17.9%, tool schemas
17.8% (23–32% of a typical 55k–75k-token Researcher request; 67k–77k characters per call), tool
call arguments 11.3%, system prompt 6.7%. One Orchestrator Session reached 217k tokens, 62% of it
25 `softmax_api` responses averaging 21,500 characters. Only 10% of calls exceeded 128k input
tokens; there the first token came 4.1 s later on Kimi K3 and 1.0 s later on K3.1 Pro.
