# Reviewed sandbox refund execution

M2 analysis still creates a dry-run proposal. Separate CLI commands now let the
local operator approve that exact proposal and execute it through M1. The model
does not call Stripe and cannot approve or execute a refund. No HTTP refund route
has been added.

## Review, approve, execute, verify

Review the saved analysis, including payment ID, currency, and amount in cents:

```bash
.venv/Scripts/python.exe -m payresolve m2 show ANALYSIS_ID
```

Only a completed `propose_refund` decision is approvable. Repeat its exact values
to create a durable local approval; this command makes no API calls:

```bash
.venv/Scripts/python.exe -m payresolve m2 approve ANALYSIS_ID --payment-id pi_YOUR_PAYMENT --amount 500 --currency usd
```

When ready to submit the approved **sandbox refund**, run:

```bash
.venv/Scripts/python.exe -m payresolve m2 execute ANALYSIS_ID
```

Execution retrieves fresh Stripe state, validates the payment and remaining
balance, submits the fixed approved amount with a durable idempotency key, and
retrieves refund/payment state to verify the result. The approved amount never
changes automatically, including proposals originally extracted as "full".
A remaining-balance change that makes the amount ineligible blocks execution.

Inspect status locally or refresh it with read-only Stripe requests:

```bash
.venv/Scripts/python.exe -m payresolve m2 execution ANALYSIS_ID
.venv/Scripts/python.exe -m payresolve m2 verify ANALYSIS_ID
```

`verified` means M1's final-state checks passed. `pending`, `failed`, or
`unresolved` do not mean successful completion. CLI exit codes are 0 for verified
or approved, 2 for pending, and 1 for failed/unresolved/errors. `show` preserves
the original dry-run analysis with `executed=false`; use `execution` or `verify`
for the separate execution outcome.

## Retries and local records

The additive `refund_approvals` table binds one intake request to one analysis,
one approved parameter set, and one generated operation ID. Repeating approval
with identical values returns the existing approval. Another analysis of the
same intake request cannot receive a second approval. Execution retries reuse
the same analysis and operation ID; never make a new intake request to work
around an uncertain outcome.

Approval is committed before the M1 workflow can send a POST. M1 separately
commits immutable Stripe parameters and the idempotency key before submitting.
If a response is lost before the Stripe ID is saved, repeat `execute` with the
same analysis ID within M1's 23-hour replay window. If the Stripe ID is known,
recovery retrieves it rather than posting again. `verify` never submits a refund;
if the Stripe ID is unknown, it instructs recovery through the original operation.
After the replay window expires, manual reconciliation is required.

Approvals are immutable and local. Cancellation/replacement is not implemented.
This is a single-operator prototype, not authenticated multi-user authorization.
Separate intake requests can represent the same real-world business request;
business-level deduplication remains future work. Explicit developer M1 commands
remain available independently. Preserve the local database for retry history.

## Verification

Offline tests cover exact-value approval, non-proposal rejection, approval before
execution, no API calls during approval, no model calls during execution, fresh
balance/state checks, restart replay, concurrent approval/execution, duplicate
analyses of one request, lost-response recovery, pending outcomes, and read-only
verification. They use temporary databases and a stateful Stripe simulator.
All 79 regression tests passed on Python 3.15.1. No dependencies were added.

The previous M1 workflow already passed real sandbox refund checks. This new
approval bridge has not submitted a real sandbox refund; use the explicit review
and approval sequence above for the first manual end-to-end execution.

Sources: [Stripe refunds](https://docs.stripe.com/api/refunds/create) and
[idempotent requests](https://docs.stripe.com/api/idempotent_requests).
