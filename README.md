# PayResolve / Stripe Refund Agent

CAP6938 project implementing an LLM-assisted refund workflow with Python,
SQLite, and Stripe's sandbox. The local Flask API provides persistent request
intake and a read-only Stripe connection check. M1 adds explicit CLI commands
for controlled sandbox payments, validated refunds, and final-state verification.
M2 adds Gemini extraction, persisted dry-run proposals, and separate explicit
approval/execution commands. Natural-language requests stay `received`;
analysis does not execute a refund. See [reviewed execution](docs/reviewed-execution.md).

## Run on Windows with Git Bash

The existing `.venv` uses Python 3.15.1. Dependencies were checked with a
wheel-only resolver dry run before installation and tested on that interpreter.
SQLite and the test runner are part of Python. No separate database installation
is needed.

```bash
# From the repository root. Activation is optional with this explicit path.
.venv/Scripts/python.exe --version
.venv/Scripts/python.exe -m pip install --dry-run --only-binary=:all: -r requirements.txt
.venv/Scripts/python.exe -m pip install --only-binary=:all: -r requirements.txt
.venv/Scripts/python.exe -m pip check
```

`requirements.in` lists direct dependencies; `requirements.txt` pins the complete
verified set. Recheck compatibility before changing those pins. See
[verification notes](docs/verification.md) for versions, sources, and test results.

If `.env` does not exist, copy `.env.example` to `.env`. Open `.env` locally and
set `STRIPE_SECRET_KEY` to a secret key from your Stripe sandbox. A restricted
test key needs **Balance: Read** for the connection check, plus PaymentIntent,
Charge, and Refund permissions for the M1 operations used. Existing environment
variables take precedence over `.env` values. Leave the key empty for offline use.

```bash
.venv/Scripts/python.exe -m payresolve --check-stripe
# Expected with a valid sandbox key: {"status": "connected", "livemode": false}

.venv/Scripts/python.exe -m payresolve
# http://127.0.0.1:8000 ; Ctrl+C to stop
```

The development server binds to loopback with debug mode and reloading disabled.
This prototype has no user authentication; keep it local and use synthetic data.
The default database is `instance/payresolve.db`, relative to the repository root.
Override `DATABASE_PATH` in `.env` if needed. SQLite files and `.env` are ignored
by Git; never commit API keys or enable verbose SDK logging with real credentials.

## API

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | Checks the backend and SQLite; does not contact Stripe. |
| POST | `/requests` | Stores a JSON `message` and returns a UUID, UTC timestamp, and `received` status. |
| GET | `/requests/<id>` | Retrieves a stored request; unknown IDs return 404. |
| GET | `/stripe/status` | Reads Stripe's balance endpoint and requires `livemode=false`. |

```bash
curl http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/requests \
  -H 'Content-Type: application/json' \
  -d '{"message":"Refund the duplicate $39.99 payment for order 4812."}'
# Copy the returned UUID into this URL:
curl http://127.0.0.1:8000/requests/RETURNED_UUID
curl http://127.0.0.1:8000/stripe/status
```

`POST /requests` accepts only `message`, with 1–4000 characters after trimming
outer whitespace and a 16 KiB body limit. Messages are stored as text, never
executed as instructions or SQL. Each successful POST creates a new intake record;
it does not initiate a refund. Missing credentials leave intake usable and return
503 from `/stripe/status`. Live/publishable/malformed keys are rejected at startup.
Stripe errors return fixed messages without the key or upstream response body.

## Verify

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m pip check
git diff --check
git check-ignore .env .venv/pyvenv.cfg instance/payresolve.db
```

Tests use temporary SQLite databases and simulated Stripe responses; they do not
read your `.env` or contact Stripe. `--check-stripe` is the separate real-network
check. It makes only a balance retrieval request and does not create payments or
refunds. A false `livemode` confirms test mode; the key determines which sandbox.

## Next increments from the proposal

M1 is implemented and verified. See [M1 usage and results](docs/m1.md) for the
command reference, retry rules, and actual sandbox fixture results. To inspect
the stored fixtures without contacting Stripe:

```bash
.venv/Scripts/python.exe -m payresolve m1 list-payments
```

The first M2 increment is implemented. Set `GEMINI_API_KEY` in your local `.env`
and `GEMINI_MODEL=gemini-3.8-flash`. `.env.example` is the shareable template with
empty key fields; `.env` contains your actual secrets and is ignored by Git.
Use only synthetic requests. See [M2 usage and verification](docs/m2.md).
For analysis-only availability fallback, optionally set
`GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite`. Flash-Lite passed all 20 development
cases. Evaluations ignore fallback so their model attribution stays explicit.

```bash
.venv/Scripts/python.exe -m payresolve m2 analyze --message 'Refund USD 5 from pi_YOUR_SANDBOX_PAYMENT'
.venv/Scripts/python.exe -m payresolve m2 analyze --request-id YOUR_REQUEST_UUID
.venv/Scripts/python.exe -m payresolve m2 show YOUR_ANALYSIS_UUID
# Separate live-model evaluation, with fixed synthetic payment state:
.venv/Scripts/python.exe -m payresolve m2 eval --limit 2
```

1. Add M2 clarification handling. Reviewed execution through the deterministic
   M1 workflow is implemented; its first manual sandbox run remains to be performed.
2. M3–M5: labeled benchmark cases, recovery and failure injection, audit events,
   latency/token/cost measurements, and final Stripe-state evaluation.

The supplied proposal is design context; the full agent and evaluation pipeline
are not implemented yet.

## GitHub checkpoint

`.env`, `.venv`, local databases, and generated reports are ignored. Commit the
source, tests, documentation, dependency pins, and empty-key `.env.example`:

```bash
git add .gitignore README.md .env.example payresolve tests evals docs requirements.in requirements.txt
git diff --cached --stat
git commit -m "Add validated refund proposals and reviewed sandbox execution"
```

There was no Git remote configured during the checkpoint inspection. After
creating an empty GitHub repository, substitute its actual URL below:

```bash
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin HEAD
```

Never force-add `.env` or the database. The configured Gemini and Stripe keys
were absent from Git-visible files during the inspection; recheck if you add files.
