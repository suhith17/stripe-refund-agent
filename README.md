# PayResolve

CAP6938 semester project: a local refund chat using Python, Flask, SQLite, Gemini
and Stripe sandbox. The agent finds a customer's purchase, asks for missing details,
and prepares a validated proposal. The customer confirms the exact purchase and
amount once; the backend submits, verifies and recovers the operation. Saved cases
and a visible action timeline explain what happened.

**Submission status:** application and evaluation evidence are included. The
eight-page report is prepared and submitted separately by the author; a final
demonstration recording is not included. This is a local USD-only prototype with a demo customer selector,
not production authentication.

## Start here

| File | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | Agent loop, memory, confirmation boundary and module map |
| [Final study](docs/final-study.md) | Frozen protocol, actual results, limitations and reproduction |

The older single-message benchmark is separate from the final
conversation-memory study.

## Repository layout

| Folder | Keep for |
| --- | --- |
| `payresolve/` | Application, agent, payment workflow, CLI and web assets |
| `tests/` | Offline regression checks and the scripted acceptance demo |
| `tools/` | Setup/backup checks, dataset-label preflight, study continuation and results export |
| `evals/conversations/` | Runnable conversation datasets and the frozen source archive |
| `evals/conversations/results/` | Sanitized measured outcomes, including failures and later repairs |
| `evals/conversations/protocols/` | Study freezes and continuation amendments |
| `evals/benchmark/`, `evals/recovery/` | Earlier experiments still used by CLI commands and tests |
| `docs/` | Architecture and final experimental study |

Local `.venv/`, `instance/`, caches and `.env` are ignored, not submission files.
The raw runs in `instance/` remain useful local evidence; do not delete the live
database or reset demo balances as part of preparing a GitHub ZIP.

## Setup

Tested environment: **Windows, Git Bash, Python 3.15.1**. SQLite is bundled with
Python. Preserve an existing `.venv`; check compatibility before installing or
changing dependencies. Commands below run from the repository root.

```bash
# Fresh checkout only:
py -3.15 -m venv .venv
.venv/Scripts/python.exe --version
.venv/Scripts/python.exe -m pip install --dry-run --only-binary=:all: -r requirements.txt
# Install only if the resolver check succeeds:
.venv/Scripts/python.exe -m pip install --only-binary=:all: -r requirements.txt
.venv/Scripts/python.exe -m pip check

# Only if .env does not already exist:
cp -n .env.example .env
```

On macOS/Linux, use the intended installed Python to create the environment and
replace `.venv/Scripts/python.exe` with `.venv/bin/python`; repeat the compatibility
check there. Those platforms have not been verified by this project.

For live chat, set `GEMINI_API_KEY` and a **Stripe sandbox** `STRIPE_SECRET_KEY` in
your local `.env`. The default model is `gemini-3.5-flash-lite`. An optional chat
backup is `gemini-3.1-flash-lite` via `AGENT_FALLBACK_MODEL`; enable it only if your
account has access. Set `GEMINI_REQUESTS_PER_MINUTE` to your account's limit (the
observed project limit was 15). The limiter does not enforce token/day quotas or
control other apps. Conversation evaluation never uses the backup.

### Private credential handoff for grading

Credentials are intentionally absent from Git and GitHub's ZIP. The project author
must deliver a temporary Gemini key and a Stripe **test-mode** secret key separately
through the course's private submission channel. Do not put either value in the
repository, README, report, GitHub issue, or submission comment visible to other
students. The grader should copy `.env.example` to `.env` in the repository root and
fill only these entries:

```dotenv
GEMINI_API_KEY=provided_gemini_key
STRIPE_SECRET_KEY=provided_stripe_test_key
```

The remaining defaults in `.env.example` are sufficient for the demo. This command
checks that both values loaded without printing either secret:

```bash
.venv/Scripts/python.exe -c "from payresolve.config import Settings; s=Settings.from_environment(); print({'gemini_key_loaded': bool(s.gemini_api_key), 'stripe_test_key_loaded': bool(s.stripe_secret_key), 'model': s.gemini_model})"
```

Both `*_key_loaded` values should be `True`. Revoke or rotate the shared keys after
grading. Offline tests and the scripted acceptance demo below require no keys.

## Run the customer demo

```bash
# Creates five sandbox payments and three demo customers; no refunds:
.venv/Scripts/python.exe -m payresolve demo add-customer --namespace presentation-nov9
.venv/Scripts/python.exe -m payresolve
```

Open **http://127.0.0.1:8000/demo**, select customer A for that namespace, and try
`Refund the headphones I bought last week.` Then answer `Friday. Just refund $1.`
Review the card before confirming. **Verified** means retrieved Stripe state
matched the confirmed operation; the timeline shows the actual tools and result.

SQLite initializes automatically at `instance/payresolve.db`. Cases survive
restarts; reselect the same customer if the session resets. Restart after code or
configuration changes. Recovery runs while the app is running.

Reseeding preserves existing payments and balances. Use a **new namespace** for
fresh demo purchases; it does not restore refunded funds. Evaluation still uses
two customers and three orders. The older operator workspace at `/` is retained
for development; `/demo` is the main product.

## Offline grading and checks

These checks use scripted models, temporary databases and simulated payments.
They need no API keys and make no provider calls:

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
.venv/Scripts/python.exe -m tests.acceptance_demo
.venv/Scripts/python.exe tools/check_setup.py
.venv/Scripts/python.exe -m payresolve.conversation_eval validate
```

The expanded development dataset has 86 executable scenarios from the 100-case
design inventory; 14 unsupported candidates are listed in its
[readiness audit](evals/conversations/results/expanded-readiness.json). The expanded
model study is recorded below; the dataset is authored development
coverage, not an unseen holdout. To check its fixture/payment labels
offline (no model calls):

```bash
.venv/Scripts/python.exe tools/check_conversation_dataset.py --dataset evals/conversations/expanded-development.json
```

This checks payment eligibility, not natural-language interpretation or complete
conversations. The audit records overlaps, a known full-refund wording limitation,
and the absence of independent human label review.

The acceptance demo covers restart, lost-response recovery, repeated confirmation
and revised amounts. `check_setup.py` checks an isolated copy using the existing
interpreter; it is not a fresh dependency installation. Generated evidence stays
under ignored `instance/` directories.

For a **real Gemini / simulated-payment** development run:

```bash
.venv/Scripts/python.exe -m payresolve.conversation_eval run --limit 1 --repeats 1 --max-requests 20
```

That command uses model quota but never calls Stripe. Preserve failed runs.
The explicit cap covers HTTP request reservations across the run and its
continuations. Interrupted/unknown sends remain counted. Twenty is an example
development cap, not a promise that the run fits or a measurement of available
quota. Reports distinguish budget stops from provider failures and leave untouched
trials unattempted. The cap does not reset daily or account for other applications;
choose it within your remaining allowance. Keep the original run directory when
continuing because it owns the shared budget ledger.

## Results and reproducibility

**Final repaired-version collection: 151/172 passed (87.8%); all scheduled trials
attempted, zero untouched.** It covers 86 authored scenarios in 19 families,
with both conversation-history and structured-memory modes. Retained failures:
20 provider failures and one behavior failure. History passed 76/86; structured
memory passed 75/86. No general memory advantage is established.

See [expanded-results-v5.json](evals/conversations/results/expanded-results-v5.json)
and the [study record](docs/final-study.md). All previous attempts and rate-limit
stops remain preserved. Collection completed through declared continuations after
a reported billing change and replacement API key; the model assignments, prompt,
oracle, datasets and original request budgets stayed fixed. These provider/account
changes are disclosed, so the result is not an uninterrupted single-account run.

No wrong-payment proposals, unexpected proposals, duplicate executions or
unauthorized effects were observed in the repaired-version collection. These are
bounded observations with live Gemini and simulated payments, not safety guarantees.
The conversation study made zero Stripe calls. Labels lack independent human
review and the scenarios are exposed development cases, not an unseen holdout.

The original complete prompt-v8 study remains **137/172** in
[expanded-results-v1.json](evals/conversations/results/expanded-results-v1.json).
The repaired-version E48 stop snapshot remains **133/148 attempted**, with 24
then untouched, in [v2](evals/conversations/results/expanded-results-v2.json).
Intermediate v3/v4 exports are cumulative checkpoints, not additional datasets;
never add their counts. The separate repair smoke is **5/6**, and the earlier
nine-scenario repeated comparison remains **42/54**. Their failed outcomes are
preserved in the study record.

Reproduce the measured code from
[frozen-expanded-v2.zip](evals/conversations/frozen-expanded-v2.zip) and its
[freeze](evals/conversations/protocols/expanded-freeze-v2.json), with continuation
conditions documented in the v3/v4/v5 protocol files. Do not pool the original
v8 result with the repaired v9 result or interpret their difference as an isolated
causal effect of memory or repairs.

Final offline checks: **298 tests passed** after the post-study citation repair; a clean source copy with an empty
database passed startup, route and persistence checks using the existing Python
3.15.1 environment. Dependency consistency passed; no fresh install was performed.

The working application includes the E52 same-order citation repair, tested offline.
The frozen archive preserves the pre-E52 code measured in the 151/172 study.
Live results require the TA's own provider credentials and available model access;
provider availability and generated outputs may differ. No credentials or existing
database are required for the offline tests and scripted acceptance workflow.

## GitHub and the grading ZIP

**The simplest option is GitHub's Code → Download ZIP**, after committing and
pushing the cleaned repository. GitHub downloads repository files, not uncommitted
local edits. Ignored local files stay out unless they were previously tracked.
Review changes before committing and pushing:

```bash
git status --short
git add -A
git diff --cached --stat
git diff --cached --check
git commit -m "Prepare PayResolve source and evaluation submission"
git push -u origin HEAD
```

Open the intended branch on GitHub, choose **Code → Download ZIP**, and submit
that archive when the report and other required deliverables are finished.
Never force-add ignored keys or databases, and do not ZIP the entire local folder.

There is no separate ZIP-builder requirement. Redownload from GitHub after final
edits; a source snapshot does not imply the report is finished. Generated local
archives remain ignored and should not be committed.

`requirements.in` lists direct dependencies; `requirements.txt` pins the verified
set. Older command references are preserved in the development history.
