# Initial implementation verification

This records the pre-M1 baseline. See [M1 verification](m1.md) for the implemented
payment/refund workflow and its current results.

Checked on 2026-10-30 on Windows AMD64, using the existing `.venv`.

## Repository and proposal review

The starting repository contained README.md, .gitignore, and an untracked virtual
environment. No application, dependency manifest, or AGENTS.md was present.
The supplied `cap6938_project_proposal.pdf` was read locally using the existing
Git for Windows `pdftotext`; no PDF dependency was installed or document uploaded.

The proposal describes PayResolve's extraction → retrieval → decision → validation
→ refund → verification pipeline, sandbox-only experiments, deterministic refund
constraints, and component/end-to-end evaluation. It is treated as project context,
not as authorization to implement every milestone in this first increment.
The requested backend, storage, and sandbox connection are the implemented scope.
No agent framework, model dependency, frontend, payment creation, or refund
execution is included yet.

## Compatibility check before installation

- Interpreter: CPython 3.15.1, Windows AMD64.
- Bundled SQLite: 3.53.4; pip: 26.2.2.
- Initial environment contained only pip.
- Direct dependencies: Flask 3.1.3, stripe 16.1.0, python-dotenv 1.2.4.
- Flask and Stripe declare Python >=3.9; python-dotenv declares Python >=3.10.
- A resolver dry run using `--only-binary=:all: --report dependency-check.json`
  accepted the full dependency set on this interpreter before installation.
- MarkupSafe 3.0.4 and charset-normalizer 3.5.2 resolved to
  `cp315-cp315-win_amd64` wheels. All other packages resolved to universal Python
  wheels. No source compilation or interpreter replacement was needed.
- The 15 resolved packages were pinned in requirements.txt and installed in .venv.

Package metadata and wheel resolution establish installability, not exhaustive
upstream support for every Python 3.15 behavior. The runtime checks below verify
the operations used in this increment. Re-run the dry run and tests on upgrades.
The local resolver report is ignored because it includes environment-specific data.

## Incremental verification

1. Backend: dependency installation completed; `pip check` passed. Two initial
   health/JSON-error tests passed. An earlier smoke command ran before installation
   had finished and could not import Flask; rerunning after completion passed.
2. SQLite intake: nine tests passed, including create/retrieve after app recreation,
   Unicode/SQL-like text, invalid input, unknown IDs, and the request size limit.
3. Stripe adapter: all 16 combined tests passed. Tests cover live-key rejection
   before client creation, missing keys, restricted test keys, SDK balance request
   construction/deserialization, mode validation, sanitized upstream failures, and
   ensuring intake/health never call Stripe.
4. Actual sandbox connection: `python -m payresolve --check-stripe` returned
   `{"status": "connected", "livemode": false}` using the locally configured key.
   Only GET /v1/balance was called. No key or balance amounts were printed.
5. Running HTTP server: GET /health returned 200; a synthetic POST /requests
   returned 201; GET of the returned Location matched the stored record. The
   smoke-test server was stopped afterward. Its synthetic record remains in the
   ignored local database.
6. Git Bash successfully ran `.venv/Scripts/python.exe --version` and reported
   Python 3.15.1. `pip check` and `git diff --check` passed. Git ignored .env,
   .venv, the database and its journal; a content check found the configured key
   in none of the Git-visible files.

Sandboxed package lookup, temporary-directory access, Git Bash startup, and Stripe network access
were restricted by the execution environment. The necessary commands succeeded
after permission-controlled reruns outside that sandbox. These restrictions were
not dependency or application failures.

## Stripe integration choices and primary sources

- [Testing Stripe integrations](https://docs.stripe.com/testing) and
  [Sandboxes](https://docs.stripe.com/sandboxes): use sandbox credentials and
  synthetic test data; general sandboxes are recommended for new integrations.
- [Agents and AI on Stripe](https://docs.stripe.com/agents): reviewed as the second
  reference in the proposal. Agent tooling is not needed for this backend increment.
- [Authentication](https://docs.stripe.com/api/authentication): keep credentials
  server-side. The app accepts only `sk_test_` or `rk_test_` keys, loaded locally.
- [Stripe Python SDK](https://github.com/stripe/stripe-python): use `StripeClient`
  with a 10-second HTTP timeout and one network retry. The pinned SDK's default
  API version is `2026-10-28.endive`.
- [Retrieve balance](https://docs.stripe.com/api/balance/balance_retrieve): the
  read-only check requires `livemode` to be the boolean false. A missing or
  malformed mode fails closed. It does not distinguish individual sandboxes.
- [Create a refund](https://docs.stripe.com/api/refunds/create): future refunds
  must reference a charge or PaymentIntent and use integer amounts in the smallest
  currency unit, bounded by the unrefunded amount. No refund endpoint exists yet.
- [Idempotent requests](https://docs.stripe.com/api/idempotent_requests): persist
  operation keys before future Stripe writes; retries must reuse the same key.
  Repeated intake submissions currently produce separate records, not refunds.
- [Flask installation](https://flask.palletsprojects.com/en/stable/installation/):
  supported Python range and dependency overview.

## Current limits

These initial-backend notes are historical. See [M1](m1.md) for implemented
idempotency/reconciliation and [M2](m2.md) for the later Gemini compatibility
check, 60 local tests, and the currently blocked live extraction evaluation.

This is a single-user local development service. Request text is stored in the
ignored SQLite database. Authentication, ownership checks, migrations beyond the
initial schema, full audit trails, duplicate-action protection, and refund-state
reconciliation belong to subsequent increments before exposing financial actions.
The tests do not establish end-to-end refund correctness or agent quality.
