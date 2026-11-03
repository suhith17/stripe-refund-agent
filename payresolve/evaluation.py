"""Live extraction evaluation against fixed synthetic payment state, never Stripe."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from .analysis import AnalysisStore, DryRunPolicy
from .config import PROJECT_ROOT
from .gemini_gateway import ExtractionError
from .intent import Intent, PROMPT_VERSION

DATASET = PROJECT_ROOT / "evals" / "refund_intents.json"


def seed(store):
    payments = {}
    with store.connect() as connection:
        for name, captured, refunded in [("good", 3999, 0), ("partial", 1500, 500),
                                          ("full", 2000, 2000), ("unpaid", 0, 0)]:
            key, payment_id = f"fixture-{name}", f"pi_{name}"
            payment = dict(id=payment_id, livemode=False, operation_key=key, currency="usd",
                           status="succeeded" if captured else "requires_payment_method",
                           paid=bool(captured), captured=bool(captured), disputed=False,
                           amount_captured=captured, amount_refunded=refunded,
                           refundable_amount=captured - refunded)
            connection.execute(
                "INSERT INTO stripe_operations (operation_id,kind,input_json,params_json,before_json,stripe_key,created_at,status) "
                "VALUES (?, 'payment', '{}', '{}', '{}', ?, 0, 'verified')", (name, key),
            )
            store.save_payment(connection, name, payment)
            payments[payment_id] = payment
    return payments


def evaluate(extractor, *, limit=2, interval=6, output_dir=None):
    if not 1 <= limit <= 20 or not 0 <= interval <= 60:
        raise ExtractionError("invalid_eval_options", "Use a limit of 1 to 20 and an interval of 0 to 60 seconds.")
    data = DATASET.read_bytes()
    cases = json.loads(data)[:limit]
    output_dir = output_dir or PROJECT_ROOT / "instance" / "evals"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{uuid4()}.json"
    report = {"status": "running", "model": extractor.model, "prompt_version": PROMPT_VERSION,
              "dataset_sha256": hashlib.sha256(data).hexdigest(), "requested_cases": len(cases),
              "created_at": datetime.now(timezone.utc).isoformat(), "cases": [],
              "payment_source": "fixed_synthetic_fixtures", "stripe_calls": 0}

    def save():
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    with TemporaryDirectory() as directory:
        store = AnalysisStore(Path(directory) / "eval.db")
        payments = seed(store)
        policy = DryRunPolicy(store, lambda payment_id: dict(payments[payment_id]))
        for index, case in enumerate(cases):
            if index:
                time.sleep(interval)
            row = {"id": case["id"], "message": case["message"], "expected": case["expected"]}
            try:
                extracted = extractor.extract(case["message"])
                actual = policy.assess(case["message"], Intent.model_validate(extracted["intent"]))
                row.update(extraction=extracted, decision=actual,
                           passed=all(actual.get(key) == value for key, value in case["expected"].items()))
            except ExtractionError as error:
                row.update(passed=False, error={"code": error.code, "message": error.message},
                           attempts=error.attempts, latency_ms=error.latency_ms)
            report["cases"].append(row)
            save()
            if row.get("error") and row["error"]["code"] not in ("invalid_model_output", "gemini_incomplete"):
                break  # Do not burn quota or retry a provider outage across the dataset.
    completed = sum("decision" in row for row in report["cases"])
    passed = sum(row["passed"] for row in report["cases"])
    report.update(status="completed" if len(report["cases"]) == len(cases) and completed == len(cases) else "incomplete",
                  attempted=len(report["cases"]), completed=completed, passed=passed,
                  pass_rate=passed / len(cases))
    save()
    return {key: value for key, value in report.items() if key != "cases"} | {"report_path": str(report_path)}
