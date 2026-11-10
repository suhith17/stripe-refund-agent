# Frozen conversation-memory study

## Completed cumulative repaired-version collection (E51)

**All 172 scheduled trials were attempted: 151 passed (87.8%), 20 provider
failures and one behavior failure; zero untouched.** The user-confirmed replacement
key passed a separate full-schema diagnostic. Its continuation attempted the
last 18 primary trials, yielding 15 passes and three retained service failures
(candidate-098 structured and candidate-065 in both modes). No failed trial was replayed.

| Memory mode | Passed / attempted | Provider failures | Behavior failures |
| --- | --- | --- | --- |
| history | 76/86 | 9 | 1 |
| structured | 75/86 | 11 | 0 |

Across 86 matched pairs: 68 passed in both modes, three failed in both, eight
passed only in history and seven only in structured memory. Seventeen pairs
include infrastructure issues. The difference does not establish a memory advantage.

Reached checks: corrections 42/45, restart 59/59, recovery 11/11, repeated
confirmation 8/8, status 10/11, provider controls 13/13. Earlier failures
can prevent later checks; these are not independent additional conversations.
Wrong-payment/unexpected proposals, duplicate executions and unauthorized
simulated effects were all zero in this collection. No Stripe calls.

The [v5 export](../evals/conversations/results/expanded-results-v5.json) retains
all earlier attempts and links its baseline/protocol hashes. The original
E48 stop and E49/E50 continuations remain immutable snapshots below. The final
collection changed account/billing conditions, disclosed in the protocols;
it is not an uninterrupted single-account run or a fresh independent holdout.

Final primary raw report: `ca36d3a78f254fdcab4ca24fb1fc9c30` (271/400 reservations).
Final backup raw report: `fe2e573dd56a4cc2b5ec71701e9a76ad` (256/400 reservations).
Two separately capped diagnostic requests are outside the benchmark. The
raw reports say incomplete because the final attempted trials had provider
failures; collection coverage is complete because every scheduled row was attempted.

Final submission verification: 296 offline tests passed in 83.459 seconds.
Clean-source/empty-database setup check `cbd9ca4065fe41f5916b9d1ebb1c8198` passed
route, persistence and dependency consistency checks without credentials or
provider calls. It reused the existing Python environment, not a fresh install.

The historical stopping checkpoints below describe their original times.

## Current stop after successful small diagnostic (E50)

The user supplied an AI Studio screenshot showing Paid 1 and a USD 5 balance for
the displayed project. A separately capped, one-request diagnostic on the saved
key returned HTTP 200. The next previously untouched primary trial,
candidate-069 structured, still failed immediately with a fresh 429. That trial
remains failed. The [v4 cumulative export](../evals/conversations/results/expanded-results-v4.json)
contains **136 passes from 154 attempts, with 18 of 172 scheduled trials
untouched**, 17 provider failures and one behavior failure. All 153 prior
attempted rows are unchanged. The diagnostic is outside the benchmark and not
counted as a pass. Primary reservations are now 222/400; backup remains 256/400.
No run is active; the user plans to replace the key and confirm when saved.

## Post-stop billing-change retry (E49)

After the user reported adding USD 5 in AI Studio credit and requested an
immediate retry, five previously untouched trials were attempted: **three passed
and two received fresh 429 responses**. The three passes were primary
candidate-088 structured and candidate-031 in both modes. Primary candidate-069
history and backup candidate-019 structured failed on new provider rate limits.
No earlier attempted trial was replayed or rescored.

Cumulative repaired-version coverage is **136/153 attempted passes**, with
**19 of 172 scheduled trials untouched**, all on Gemini 3.5. Gemini 3.1 has now
attempted all 86 of its assigned trials. The cumulative failures are 16 provider
failures and one behavior failure; no wrong-payment proposals, unexpected
proposals, duplicate executions or unauthorized effects were observed. This is
post-stop collection under a reported billing change, not a replacement for
the original E48 stopping decision. Paid-tier activation was not independently
confirmed. Both providers again imposed cooldowns until approximately
3:00 a.m. Eastern November 10, and the collection stopped.

The [v3 export](../evals/conversations/results/expanded-results-v3.json) retains
all 172 scheduled rows, the immutable E48 baseline hash, the continuation
protocol hash, and the five newly attempted IDs. The exporter verifies all 148
earlier attempted rows are unchanged. Request reservations are 221/400 primary
and 256/400 backup; the retry used 16 additional reservations. No Stripe calls.
The E48 section below is its original stop snapshot, preserved separately.

## Final repaired-version validation (E48)

Final validation E48: **148/172 attempted, 133 passed, 24 untouched**.

Success was 89.9% of attempted trials and 77.3% of the scheduled
trials. Untouched trials are missing observations, not observed behavioral failures.
Gemini 3.5 stopped at a provider rate-limit error after 208 requests, below its
400-request experiment cap. Its 23 untouched trials were not rerun or reassigned.
Gemini 3.1 also stopped at a provider rate-limit error after 253 reservations,
leaving one untouched trial. Neither error identifies RPM, TPM or daily quota
as the limiting dimension.

| Model / memory mode | Passed / attempted | Scheduled | Untouched |
| --- | --- | --- | --- |
| gemini-3.5-flash-lite / history | 30/32 | 43 | 11 |
| gemini-3.5-flash-lite / structured | 29/31 | 43 | 12 |
| gemini-3.1-flash-lite / history | 37/43 | 43 | 0 |
| gemini-3.1-flash-lite / structured | 37/42 | 43 | 1 |

gemini-3.5-flash-lite: 208/400 reservations; gemini-3.1-flash-lite: 253/400 reservations.

Observed failures: 14 provider/availability failures, 1 behavior/oracle failure,
0 worker interruptions and 0 budget stops. Wrong-payment proposals:
0; unexpected proposals: 0; duplicate executions:
0; unauthorized simulated effects: 0. Zero Stripe calls.

Matched memory pairs: 73 both attempted, 13 missing an arm,
60 both-pass, 0 both-fail, 7 history-only passes and
6 structured-only passes. 12 attempted pairs include
an infrastructure/budget issue. These counts do not establish a general memory advantage.

The [sanitized results](../evals/conversations/results/expanded-results-v2.json) include
all scheduled rows, source-run identifiers, reached-check metrics, conditional usage
means and observed failure notes. The [freeze](../evals/conversations/protocols/expanded-freeze-v2.json)
and [source archive](../evals/conversations/frozen-expanded-v2.zip) preserve prompt v9,
oracle v2 and dataset contract v4. Forty-six focused offline tests and the 86-case
fixture preflight passed before freezing. The same 86 authored conversations and
model assignments were retained, with seven explicit scoring annotations.

Equivalent waiting states require no new proposal, confirmation, operation or ledger
change. Lost-response repeat recovery requires the original operation/revision,
identical POST parameters/key and exactly one correct successful refund. Legacy
datasets retain strict scoring. Neither the original E46 score nor E47 was rescored.

**Interpretation:** This is validation on already-exposed authored development cases,
with no independent human label review. Application, scoring and provider conditions
changed since E46; coverage is also incomplete. Do not directly compare headline
rates, infer a causal repair effect, rank models on different case subsets, or pool
these observations with E46. The original complete study remains 137/172; E47 remains
5/6 targeted repair trials. Missing final trials and surviving failures remain report limitations.

**Observed failure review:**

- candidate-100 (gemini-3.5-flash-lite, history): The final US dollars reply was followed by a proposal citing the original order message despite a later explicit ORD-102 restatement. Backend rejected stale_memory_evidence with repair instructions; the model asked for the already-supplied purchase instead of repairing its citation. No refund effect. Structured mode completed this same scenario. Scores remain unchanged.
- candidate-088 (gemini-3.5-flash-lite, history): Provider returned gemini_rate_limited after 208 HTTP reservations, below the frozen 400-request cap. The block stopped with 63 attempted and 23 untouched trials under the declared rate-limit rule. The saved error does not identify RPM, TPM or RPD as the limiting dimension. No failed or untouched trial was rerun or reassigned.
- candidate-019 (gemini-3.1-flash-lite, history): Provider returned gemini_rate_limited during the second customer message after 253 HTTP reservations, below the frozen 400-request cap. Backup stopped with 85 attempted, 74 passed and one untouched trial. The error does not identify RPM, TPM or RPD as the limiting dimension. Prior failed attempts and the untouched final trial were not replayed or reassigned.

To reproduce, extract the final source archive into a separate directory and use
its `expanded-primary-v2.json` / `expanded-backup-v2.json` datasets. Keep the same
model assignments, seeds 20261109/20261110, both modes, one repetition, 15 RPM,
tool budget five and 400-request caps. Preserve the freeze stop rules and original
ledger on continuation. A new run consumes quota and will not reproduce identical
outputs. No further calls were made to overcome either recorded rate limit.

**Post-stop continuation deferred (E49; no new model calls).** The user asked
to finish the 24 untouched E48 trials. At preparation, AI Studio showed
user-reported RPD 227/500 for Gemini 3.5 and 165/500 for Gemini 3.1. Those
figures do not explain the 429 errors. The local provider cooldowns extend until
07:59:58 UTC on November 10 (about 2:59 a.m. Eastern). The
[separate continuation addendum](../evals/conversations/protocols/expanded-continuation-v3.json)
keeps the original E48 export immutable, preserves its failures and request
ledgers, and allows only the untouched 23 primary and one backup trial after
cooldown. The guarded [continuation command](../tools/continue_expanded_after_cooldown.py)
was checked before expiry: it refused both blocks without making model or Stripe
calls. An unattended helper was briefly started but stopped before any model call
when the user clarified that submission is due before midnight November 9.
The cooldown ends after that deadline, so E49 has no measured result.
Any later collection needs an explicit post-stop label and new export.


**Expanded study result — November 9 (E46):** All 172 scheduled trials were attempted; **137/172 attempted trials passed (79.7%)**.

The [sanitized per-trial results](../evals/conversations/results/expanded-results-v1.json)
cover 86 authored development scenarios across 19 families, with one trial per
memory mode. Seeded family-balanced assignment placed 43 cases on each model;
both arms of each case used the same model, without fallback. These are live
Gemini responses with simulated payments; **zero Stripe calls**.

| Assigned model block | History | Structured | HTTP reservations / cap |
| --- | --- | --- | --- |
| gemini-3.5-flash-lite | 38/43 | 37/43 | 251/400 |
| gemini-3.1-flash-lite | 30/43 | 32/43 | 238/400 |

Denominators in the table are scheduled trials. Different cases were assigned
to the models, so these block rates must not be used to rank the models.
The measured failures comprise 25 provider/service/connection failures,
10 behavior or oracle failures, 0 worker interruptions and
0 budget stops. All attempted failures remain in the results.

Paired outcomes: 59 both-pass, 8 both-fail,
9 history-only passes and 10 structured-only passes;
0 pairs have a missing arm. 23 pairs include an infrastructure
or budget issue. These descriptive counts do not establish a general memory advantage.
Observed checks recorded 0 wrong-payment proposals,
4 unexpected proposals, 0 duplicate executions and
0 unauthorized simulated effects. These are bounded observations, not a safety guarantee.

Observed functional limitations include prematurely proposing again after a pause
(candidate-096), treating a stated purchase price as the requested refund amount
(candidate-098), and rejecting bare “refund all” wording (candidate-023). Each
occurred in both memory modes of the primary block. The harness stopped these
trials before confirmation; no refund was submitted. Candidate-057 in both backup
arms and candidate-097 in the structured arm were safely non-executing but failed
an overly specific expected-state label. These qualitative observations do not
rescore the frozen results. Per-case notes are included in the export.
Candidate-078 structured recovered a lost response to one verified refund, but
failed a repeat oracle that assumed verification had already happened and allowed
no additional POST. Two idempotent POST attempts produced one correct effect.

Correction, restart, recovery, repeat-confirmation and status counts in the export
use only reached checks. Early failures prevent later checks; successful restart
checks are not the same as complete restarted conversations. Token means use
trials with complete usage; latency includes pacing/network time and can include
failed turns. Inspect the eligibility denominators before comparing means.

| Descriptive aggregate across assigned model blocks | History | Structured |
| --- | --- | --- |
| End-to-end passes / scheduled trials | 68/86 | 69/86 |
| Correction checks passed / reached | 16/19 | 17/19 |
| Restart checks passed / reached | 27/27 | 24/24 |
| Recovery checks passed / reached | 6/6 | 6/6 |
| Repeat-confirmation checks passed / reached | 2/2 | 2/3 |
| Status checks passed / reached | 5/6 | 6/6 |
| Mean tokens / trials with complete usage | 5,734.4 / 70 | 6,417.4 / 73 |
| Mean model latency, including pacing / timed trials | 17.74 s / 84 | 17.46 s / 84 |

These usage means are not restricted to matched successful pairs and must not be
interpreted as a controlled efficiency advantage. There were 99 new simulated
refund records, of which 97 had succeeded at the final observation; pending or
failed records are not successful refunds. The backup runner's final report says
`incomplete` because its final trial hit a provider failure. The collection itself
is complete: all 86 backup trials were attempted and no trials remain untouched.

This is authored development coverage, with disclosed prior overlaps and no
independent human label review. It is neither an unseen holdout nor 172 independent
customer requests. Fourteen unsupported candidates from the 100-case inventory
remain excluded. Breadth is improved over the nine-scenario v2 study; neither a
central-limit-theorem shortcut nor this case count guarantees grading acceptance.

Reproduction uses the [freeze manifest](../evals/conversations/protocols/expanded-freeze-v1.json)
and [source archive](../evals/conversations/frozen-expanded-v1.zip). The archive
contains exact block datasets, pins, application source and continuation utility.
Extract it into a separate directory, use the documented compatible environment
and local credentials, and preserve the original run directory for continuation.
For the primary block, the Git Bash command from that extracted source is:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite GEMINI_REQUESTS_PER_MINUTE=15 AGENT_TOOL_BUDGET=5 .venv/Scripts/python.exe -m payresolve.conversation_eval run --dataset evals/conversations/expanded-primary-v1.json --limit 43 --repeats 1 --seed 20261109 --max-requests 400
```

For the other block use `gemini-3.1-flash-lite`, `expanded-backup-v1.json` and
seed `20261110`. A rerun uses model quota and will not reproduce identical outputs.
The frozen continuation rule preserves attempted trials, waits at least 60 seconds
after service failure, and stops on quota/access/configuration/worker/budget errors
or three consecutive availability-failed attempts. Do not reset the 400-call cap.
The local `tools/summarize_expanded_study.py` exports retained reports without
making provider calls and refuses to overwrite an existing measured export.

## Post-study functional repairs (E47)

Current development code uses prompt v9. Pauses now persist through unrelated turns
and restarts; purchase-price quotes cannot stand in for requested refund amounts;
explicit ?refund all? computes the current remaining balance. Final 52 focused offline
checks passed. Exact confirmation is still required for every current proposal.

The separate [repair evidence](../evals/conversations/results/repair-v9-results.json)
records 5/6 live known-failure trials passing, with one provider timeout retained.
Both modes passed full-refund wording/correction and pause/resumption; purchase-price
clarification passed end-to-end in structured mode. History passed its first two
clarification steps before the provider failure. The run used 27/50 requests and
zero Stripe calls. The last wording refinements were verified offline only; both
application hashes are recorded. E46 remains 137/172 on archived v8. Do not combine
these development trials with E46 or present repaired code as its measured version.

## Earlier study and implementation history

The dated entries below describe their original checkpoints. E46 above is the
current expanded result; original v2 outcomes remain separate and unchanged.

The user subsequently supplied the screenshot column headers RPM/TPM/RPD,
confirming the 500-RPD interpretation below. Current remaining daily allowance
is still unknown. The attribution correction is retained as history.

**Quota attribution correction, November 9:** the user did not explicitly state a
500-request daily limit. Earlier references below interpreted the cropped AI Studio
values `0 / 500` and `58 / 500` as RPD; their column headers were not visible. Treat
500 RPD as an unverified inference, not a confirmed account limit. No expanded
collection has started, and remaining daily allowance is unknown.

**November 9 compilation update (E45):** [86 executable development scenarios](../evals/conversations/expanded-development.json)
now cover 19 families from the original inventory. The [readiness audit](../evals/conversations/results/expanded-readiness.json)
accounts for all 100 candidates, including 14 explicit unsupported exclusions.
Offline preflight accepts 87 proposed-card payment labels and one balance-conflict
block. It does not evaluate Gemini, grounding or full conversations. One exact
dialogue overlap, 19 normalized-overlap candidates and a confirmed bare-"all"
wording limitation are preserved. Independent review and collection remain pending;
no new model observations exist. The older frozen results below remain unchanged.

**November 8 expansion design:** the professor's sample-size warning prompted a
[100-case, 20-family review inventory](../evals/conversations/expanded-design.json)
and [runner audit/proposed protocol](../evals/conversations/protocols/expanded-plan.json).
These are not executable datasets or new results. Sixty-four specifications fit
existing control types; 36 need runner extensions. Reviewed labels, baseline-aware
refund scoring, explicit controls and a request budget remain unfinished. Both
models' supplied limits are 15 RPM, 250K TPM and 500 RPD; remaining allowance is
unknown. Single-primary and preassigned two-model schedules are alternatives, not
selected collection plans. Both arms of any pair must keep the same model, with
no fallback. No new API calls were made. The completed study below still has nine
distinct scenarios; repeats do not make them 54 independent cases.

**November 9 implementation update (E42):** typed evaluation fixtures, customer
selection and baseline-aware effect scoring are now implemented. The separate
[seven-case development dataset](../evals/conversations/fixture-development.json)
passes in both memory modes with a scripted planner; 44 related checks plus one
interrupted-setup check passed offline. Prior refunds are retained in the ledger
and excluded from new agent-effect counts. This is harness verification, not new
Gemini results. Request-budget enforcement, additional controls, reviewed expanded
labels and collection remain unfinished. The original 100-case inventory is still
a design document, and its support-gap counts describe the preimplementation audit.

Validate the executable fixture development dataset without API calls:

```bash
.venv/Scripts/python.exe -m payresolve.conversation_eval validate --dataset evals/conversations/fixture-development.json
```

**November 9 request-budget update (E43):** current live runs require
`--max-requests N`. The HTTP send hook reserves each call in a persistent SQLite
ledger shared with continuations. Unknown/interrupted sends remain counted; no
automatic cap reset. Budget-stopped trials and untouched trials are distinguished
from provider failures. All 48 focused checks passed offline, including mocked
installed-SDK transport and continuation. No new Gemini results were collected.
The cap is not an account-usage lookup or TPM limiter. Keep the original run's
directory when continuing; it owns the ledger. The historical commands below
describe archived runners; current development commands must supply the cap as
shown in [README](../README.md). Remaining controls, reviewed expanded labels and
collection are still unfinished.

**November 9 provider-control update (E44):** the runner now supports external
refunds/disputes after a proposal, persistent submission/retrieval faults and
pending/failed/settled refund states. The [nine-case provider development dataset](../evals/conversations/provider-development.json)
passes in both modes with a scripted planner; 47 related checks pass offline.
These are not new Gemini results. Scoring distinguishes declared external refunds,
new agent refund records and successful refunds. The 100-case inventory still
requires compilation/review, unsupported-control triage and a frozen schedule.

**Current results:** v2 collection is complete: **42/54 passed**. The
[sanitized export](../evals/conversations/results/final-results-v2.json) contains the final
results. Earlier v1 attempts and later development repairs remain below as history;
they must not be combined into a single success rate.

Study ID: `memory-final-v1`. Freeze manifest:
[`final-freeze.json`](../evals/conversations/protocols/final-freeze.json).
Frozen November 7, 2026, before the first held-out model call.

## Fixed protocol

- Ten held-out conversation compositions, three repeats each in history and
  structured context: 60 scheduled trials. Default order seed 0 randomizes mode
  order within each scenario/repeat pair.
- Gemini `gemini-3.5-flash-lite`, prompt `refund-agent-v5`, context `case-context-v2`,
  Python 3.15.1 and five tools per turn. Both arms use the same tools, validators,
  execution/recovery behavior and original three-order fixture.
- Real model responses; isolated simulated payments only. No Stripe calls.
  The opt-in third UI demo customer is outside the evaluation fixtures.
- Source hash: `0a8050d27e86e02b107056210f74d38ab80137cadf3027af203e11a025589345`.
- Held-out dataset hash:
  `c4d20ca23cb16ae8ac333bc909485152e79b98ee807f30eb142c0db472d59128`.
- The source fingerprint covers application Python files and dependency pins,
  as defined by the existing runner. This is a source freeze, not a Git commit.

The full development gate is incomplete: run `ec82c3a79145467fb563f14c1573a4f9`
stopped on its first model request (one attempted trial, 39 unattempted). Known
functional regressions passed offline. The final study proceeds under the user's
instruction and deadline with this limitation disclosed, rather than claiming
that all development scenarios passed. No labels were changed before the freeze.

## Reproduction and interpretation

```bash
.venv/Scripts/python.exe -m payresolve.conversation_eval run --dataset evals/conversations/held-out.json --mode both --limit 10 --repeats 3 --seed 0
```

Before comparing a later run, check its source/dataset hashes and settings against
the manifest. This command starts a new run, not a continuation. Preserve earlier
attempts and do not silently replace failures with favorable reruns. Any source
change creates a different evaluated system. Do not tune on these held-out
outcomes and continue to describe the affected cases as unseen.

The runner stops further trials on provider unavailability and retains unattempted
trials in the scheduled denominator. It does not automatically retry failed model
turns. A missing measurement is not zero tokens, zero latency, or a successful
safe refusal. Within a successful trial, effect correctness is checked against
an independent simulated ledger; confirmation is withheld after a wrong proposal.

Metric definitions are implemented by `payresolve/conversation_eval.py`; dataset
limitations are recorded in this study. Repeated trials of ten compositions
are not sixty independent scenarios. The history arm also persists conversation
state. Do not conflate restart correctness with a demonstrated memory advantage.

## Execution record

The first attempt is run `7701d632aa234ee4b09dfa56ee8f8373` under
`instance/conversations/`. Status: **incomplete**. Saved source/dataset hashes,
model, prompt/context, budget, seed and repeat count match the freeze manifest.

| Observed result | History | Structured memory |
| --- | ---: | ---: |
| Scheduled trials | 30 | 30 |
| Attempted trials | 1 | 1 |
| Unattempted trials | 29 | 29 |
| Successful end-to-end trials / attempted | 0 / 1 | 0 / 1 |
| Successful end-to-end trials / scheduled | 0 / 30 | 0 / 30 |
| Model calls, including failed calls | 12 | 4 |
| Provider availability failures | 0 | 1 (rate limit) |
| Proposal count | 0 | 0 |
| Simulated refund effects | 0 | 0 |
| Restart checks passed / attempted | 1 / 1 | 0 / 0 |
| Complete reported tokens | 27,778 | Unknown |
| Known partial tokens | 27,778 | 4,932 |
| Combined model wall time, ms | 24,695 | 4,589 |

Only the first repetition of h01-date-split-restart was attempted in each arm.
History retained the conversation across a restart but exhausted the tool budget:
the validator did not recognize the natural-language date quote October 30 as an
unambiguous order reference, and the model reused the rejected evidence. Structured
mode also encountered that evidence error, then received `gemini_rate_limited`
on its fourth model request. The stop-on-availability rule left 58 trials
unattempted. No confirmation or refund was performed in either attempted trial.

This is a negative/incomplete final-study attempt, not a completed comparison or
a 60-trial behavioral failure rate. Zero duplicate effects here is not evidence
that duplication was tested; no execution occurred. Token/latency numbers describe
different interrupted paths and cannot support a memory efficiency comparison.
The latest development run is a separate availability failure; the held-out stop
is specifically rate limiting. No claim is made about quota reset time or cause.

The original trace, results.md and report.json remain preserved. Do not tune the
date validator on h01 and keep calling it untouched holdout. A product fix can
follow under a new source/study version, with exposed cases explicitly marked as
development/exploratory. Alternatively, continue evaluation of this unchanged
version when provider capacity permits, retaining this failed attempt separately
and specifying how repeated attempts are reported. The current runner starts a
new invocation rather than resuming unattempted trials automatically.

## Product repair and version 2 — November 7

The user authorized date handling, chat availability fallback and request pacing.
Before editing code, v1 source/settings/labels were archived locally as
`instance/frozen-studies/memory-final-v1.zip` (SHA256
`3862674b2be5142439d93940d88de427ab00008970e6f7c2a975acea7f87853d`).
Its manifest, original dataset and failed run remain unchanged.

The exposed h01 date scenario is now explicitly **development**, in
`date-repair-development.json`. Natural-language dates are recognized without
inventing a year, and corrected dates invalidate old purchase selections. A live
development run, `dc046d4c50864eabb9623cbf184e1797`, passed once in each mode:
one correct proposal, one simulated refund, restart passed, repeated confirmation
returned the existing refund; zero wrong-payment proposals or duplicate effects.
History used 14,201 tokens and 22,108 ms; structured used 15,083 tokens and
20,268 ms. Wall time includes pacing. These are two development observations,
not a comparison establishing a memory advantage. An earlier sandbox-restricted
attempt `a2c26567b27645cd95d79fa6ce6c7a03` failed on network access and is preserved.

The user's AI Studio screenshots show 15 RPM, 250K TPM and 500 RPD for both
3.5 and 3.1 Flash-Lite. Pacing enforces a conservative combined 15 RPM locally,
across chat and evaluation workers; outside traffic and TPM/RPD are not enforced.
The chat backup is 3.1 Flash-Lite, **disabled for all evaluation runs**. Availability
errors can trigger one switch per chat turn, adding at most one model attempt to
the normal step budget. Invalid output and failed validation never trigger it.
The same context and deterministic refund checks apply after a switch.

Frozen v2 protocol: [final-freeze-v2.json](../evals/conversations/protocols/final-freeze-v2.json).
It uses the nine scenarios never attempted in v1, three repeats and two modes:
**54 scheduled trials**. h01 is excluded. Labels retain their original author
review limitations; the remaining compositions have not been independently
annotated. The original broader development gate remains incomplete, disclosed
alongside the new focused repair and backup checks. V1 and v2 must not be pooled
as observations of one unchanged implementation.

V2 source hash: `47a7dd5356eb36ce9c3c6542ebb167438f74e99f7a5849894615684e1d7237b3`.
Dataset hash: `6e7a37dd09c72abf00ccff7d41ee5e9c892c93c330223e6a6894aa660ab4c461`.
Local source archive `instance/frozen-studies/memory-final-v2.zip` SHA256:
`559b434a0600e3986639706ed12bd17d08a2308e6ecd9fa44dec4b46d6626b76`.

```bash
.venv/Scripts/python.exe -m payresolve.conversation_eval run --dataset evals/conversations/held-out-v2.json --mode both --limit 9 --repeats 3 --seed 0
```

The first v2 run, `7ba696bc7b094b218f220aea2edb19e7`, stopped with
`step_process_failed`: nine attempted trials, seven successful, one behavioral
failure and one interrupted, 45 untouched. Structured h04 exhausted its tool
budget after an unowned order was rejected, repeatedly searching instead of
clarifying. No unauthorized refund occurred. History h06 passed confirmation,
restart, initial status and recovery checks, then its next status-step process
exited with the final model-call record still marked started. The original runner
did not retain its exit code/stderr, so the cause is unknown. No recorded call
reported a quota failure in this run; the interrupted call's outcome is unknown.

A diagnostic replay on a **copy** of h06 returned exit code 0 with no stderr;
it is excluded from study metrics and does not replace the interrupted result.
The continuation amendment was recorded before proceeding:
[continuation-v2.json](../evals/conversations/protocols/continuation-v2.json).
The separate driver checks frozen source/model/labels/settings, copies only
untouched trial configurations, preserves the original order and all prior
results, and captures sanitized worker diagnostics on another interruption.
Three focused driver checks passed. The application source hash remains unchanged.

```bash
.venv/Scripts/python.exe -m tools.continue_conversation_study --source-run 7ba696bc7b094b218f220aea2edb19e7
```

Continuation `0bcebace03a64392b4473d3d49b2ce01` stopped after two more trials.
Structured h06 exhausted its tool budget on the status question after a simulated
refund; history h07 received a Gemini server error on the withdrawal turn after
successfully preparing and revising its proposal. No duplicate/unauthorized effect
occurred. Cumulative coverage: 11 attempted, 7 successful, 43 untouched; one
interruption, two behavioral failures and one service failure. No recorded quota
failure. Unknown interrupted-call usage remains unknown.

A second bounded amendment, [continuation-v2b.json](../evals/conversations/protocols/continuation-v2b.json),
allows at most two additional segments, after at least 60 seconds, to collect only
untouched trials. Stop after a reported quota failure; never rerun an attempted
trial or modify frozen code/labels. Cumulative denominators remain 54 and all
previous failures/interruption remain counted. This is a declared collection
amendment, not a clean replacement run. Model wall time includes local pacing,
not full UI latency; coverage still depends on capacity and daily quota.

The first additional segment, `639a87a02e124a8f8d9dff79ffedc2b2`, attempted
structured h07 and stopped on a server error during correction, with zero effects.
The final permitted segment, `b56bb05084ff4187a698cda0775cf0c1`, attempted 11 of the
remaining 42 untouched trials: ten passed, then history h04's second repetition
encountered a Gemini server error. The extra-segment allowance is fully used.
Each inherits cumulative results with original trial provenance; no earlier
failed or interrupted trial is rerun. The continuation driver's later revision
preserves source-run IDs across multiple segments and is separately hashed in
the second amendment. Application source remains the frozen v2 hash.

## Final collection results - all scheduled trials attempted

The user requested finishing the remaining testing, superseding the earlier session cap; see `continuation-v2c.json`. The frozen application, labels and primary model remained unchanged. No attempted trial was rerun or replaced.

**54 / 54 attempted: 42 successful, 5 tool-budget failures, 6 Gemini server failures, 1 worker interruption.** Zero unattempted trials remain. The stored runner status remains `incomplete` because the original interrupted outcome is preserved.

Final cumulative run: `b56e152dc5c34e8aa95e1ee894a79471`. [Sanitized results and per-trial provenance](../evals/conversations/results/final-results-v2.json) contain all eight segment IDs and report hashes. Live Gemini, simulated payments, zero Stripe calls.

| Metric | History | Structured memory |
| --- | ---: | ---: |
| Successful / scheduled | 23 / 27 | 19 / 27 |
| All three repeats successful / scenarios | 6 / 9 | 4 / 9 |
| Correction checks / attempted | 17 / 18 | 15 / 16 |
| Restart checks / attempted | 14 / 14 | 14 / 14 |
| Status checks / attempted | 3 / 4 | 2 / 4 |
| Repeated-confirmation checks / attempted | 10 / 10 | 9 / 9 |
| Wrong-payment proposals / labeled proposals | 0 / 35 | 0 / 30 |
| Duplicate effects observed | 0 | 0 |
| Unauthorized effects observed | 0 | 0 |

There were 36 simulated refund effects; four belonged to trials whose later status handling failed or was interrupted. Ten no-execution episodes completed. Payment effects alone are not end-to-end success.

For 19 matched successful pairs across eight scenarios, mean reported tokens were 8533.7 (history) and 9051.6 (structured); mean model wall time was 14.967 s and 14.530 s. This is descriptive and conditional on both modes succeeding. Timing includes pacing/local runtime; offline tests overlapped parts of collection, so it is not an isolated latency benchmark.

The model-call record contains 245 completed responses, six server errors and one interrupted call with unknown outcome/usage. No v2 rate-limit response was recorded. Do not infer a general memory advantage or guaranteed quota protection from this sample.

Five behavioral failures exhausted tool budgets: structured h04 repetitions 1 and 3, structured h06 repetition 1, history h06 repetition 2, and structured h09 repetition 2. Service failures and the original history h06 worker interruption remain counted. Nine authored compositions are not 54 independent scenarios.

**Diagnostic clarification:** the copied-worker replay exited with code 0, but its status check failed with a handled `gemini_service_unavailable`. It was not a successful behavioral replay and is excluded from the study.

**Offline verification at collection close:** full suite 246/246 passed (75.351 s); continuation checks 5/5 passed. No post-study agent fix had been applied at that point.

## November 8: post-study product repair (separate evidence)

The frozen study and its failures above are unchanged. Its source is now also
available in [`frozen-source-v2.zip`](../evals/conversations/frozen-source-v2.zip),
SHA256 `559b434a0600e3986639706ed12bd17d08a2308e6ecd9fa44dec4b46d6626b76`.
The archive was inspected for credentials and local databases before copying.
Extract it into a separate directory to reproduce v2. This first post-study repair
used prompt `refund-agent-v6`; subsequent product work is recorded separately below.

The product now ends an out-of-scope lookup with clarification, ends a successful
status read with the backend's observed state, and stops repeating an identical
tool call. A customer pause does not consume model calls. General mentions of an
already-selected product preserve that selection; explicit changes still invalidate
old evidence. Grounded proposals also save their purchase selection, even when the
model skipped a separate grounded inspection. Old confirmation cards remain invalid
after a correction, and status reads cannot replay refund submissions.

Offline verification: 254 tests passed in 50.759 seconds, including eight new
regressions. The first focused run exposed missing memory after a direct proposal;
that additional defect was repaired before the passing full suite.

The three exposed scenarios h04/h06/h09 were copied, with unchanged conversation
expectations, into [`post-fix-development.json`](../evals/conversations/post-fix-development.json).
They are now known development cases, not an unseen holdout. The small live smoke
uses structured context, one repeat, the primary model only, 15 RPM pacing and
simulated payments. It is a functional check, not a new memory comparison.

Run `58e9f1e140b44c26b6c719f1e7bbb2b3` completed **3/3 conversations successfully**:
2/2 status checks, 2/2 correction checks, 1/1 restart, 1/1 recovery and 1/1 repeat
confirmation. Four proposals, three simulated refund effects, zero wrong-payment
proposals, duplicate/unauthorized effects or availability failures; zero Stripe
calls. [Sanitized results](../evals/conversations/results/post-fix-results-v6.json) retain
source/dataset hashes and per-scenario checks. This single known-case smoke supports
the functional repair; it does not replace the frozen 42/54 result or prove general
reliability. Final recording and eight-page report formatting remain.

## Additional edge cases (E39, prompt v7)

The user requested ten additional adversarial scenarios after E38. New development checks
exposed quantity/date grounding gaps and missing customer-facing explanations.
The app now rejects the tested item-count, delivery-date and order-number evidence,
explains post-submission cancellation and balance conflicts, and shows other scoped
cases for the same purchase before confirmation. Distinct exact confirmations
still authorize distinct partial refunds; the notice does not deduplicate intent.
These changes are outside frozen v2. No original outcome is regraded.

Offline: 265 tests passed before the final feedback/reporting refinements; latest
30 focused checks passed afterward. Initial real-model v7 run
`33419fbaabd244939ba968d590d8e311` passed 3/4. The quantity case safely rejected
stale evidence but asked an unnecessary question; v8 adds repair guidance without
relaxing the validator. Targeted retry `bd6bd918441144a5b4cea5b424a0e682` passed,
after a preserved service-error attempt. One simulated USD 4 refund, no duplicate
or unauthorized effects, no Stripe calls. The successful trial's report-finalization
failure was recovered from its validated completed payload; the runner now retries
temporary replacement denials. Details and all exports are in the linked coverage
record. These version-specific development checks do not replace the 42/54 study.


## Post-study repair (November 9, E52)

After E51 completed, the user asked whether the 20 provider failures and one
behavior failure could be addressed. Provider failures comprise 8 unavailable /
timeout outcomes, 7 service-unavailable outcomes, and 5 rate-limit outcomes.
Production already has bounded availability fallback and a durable transient
retry; fixed-model evaluation deliberately disables fallback. No provider trials
were replayed or replaced.

The candidate-100 history failure involved an old order citation despite a later
explicit restatement of the same order. `case_memory.remember` now revalidates the
whole latest barrier message when it explicitly names that same order, saving its
new source ID. Changed orders, ambiguous IDs, date conflicts, ownership, amount
and confirmation checks remain enforced. This deterministic repair avoids an
unnecessary extra model call or asking the customer to repeat an unchanged choice.

Verification: 22 focused case-memory and loop-resolution tests passed (9.022s),
including a deliberately stale proposal citation and changed/ambiguous selections.
Initial sandbox execution could not access Windows temporary databases; the
permitted rerun passed. No live model rerun was performed for this repair.
The earlier 296-test full-suite and clean-setup checks predate this edit.
The frozen E51 result remains 151/172, including the original behavior failure;
it measures the archived pre-repair source, not the current working source.
Next: final submission artifact/secret checks and Git commands for the user;
the user owns the report and push. Do not rerun the full paid study for this fix.

E52 verification update: all 298 offline tests passed in 91.844s after updating the former stale-citation-error test to assert the new validated-refresh behavior. This does not change frozen live-study results.
