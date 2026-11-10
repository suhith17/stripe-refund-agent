"""Decision pilots with durable checkpoints; model arms never execute refunds."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from .analysis import AnalysisStore, DryRunPolicy
from .benchmark import DatasetError, parse_dataset, read_json, validate_dataset
from .config import PROJECT_ROOT, Settings
from .gemini_gateway import ExtractionError, GeminiExtractor
from .intent import Intent, PROMPT_VERSION
from .benchmark_models import GeminiDecisionWrapper, WRAPPER_PROMPT_VERSION
from . import rules

RUNNER_VERSION = "decision-pilot-v2"
DEFAULT_DIRECTORY = PROJECT_ROOT / "instance" / "benchmarks"


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint():
    # Refuse to combine cases produced by different runner/parser/policy code.
    digest = hashlib.sha256()
    for name in ("benchmark_runner.py", "benchmark.py", "benchmark_models.py", "gemini_gateway.py",
                 "rules.py", "analysis.py", "intent.py", "operations.py", "storage.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    digest.update((PROJECT_ROOT / "requirements.txt").read_bytes())
    digest.update(platform.python_version().encode())
    return digest.hexdigest()


def fixture_policy(store, fixtures):
    """Adapt synthetic records to the same ownership/state checks used by M2."""
    payments = {}
    with store.connect() as connection:
        for index, fixture in enumerate(fixtures):
            operation, key = f"fixture-{index}", f"key-{index}"
            payment = dict(id=fixture.id, amount=fixture.amount_minor, livemode=fixture.livemode,
                           operation_key=key if fixture.identity_matches else "different-owner",
                           currency=fixture.currency, status=fixture.status, paid=fixture.paid,
                           captured=fixture.captured, disputed=fixture.disputed,
                           amount_captured=fixture.captured_minor, amount_refunded=fixture.refunded_minor,
                           refundable_amount=fixture.captured_minor - fixture.refunded_minor)
            payments[fixture.id] = payment
            if fixture.managed:
                connection.execute(
                    "INSERT INTO stripe_operations (operation_id,kind,input_json,params_json,before_json,stripe_key,created_at,status) "
                    "VALUES (?, 'payment', '{}', '{}', '{}', ?, 0, 'verified')", (operation, key))
                store.save_payment(connection, operation, payment)
            if fixture.unresolved_operation:
                connection.execute(
                    "INSERT INTO stripe_operations (operation_id,kind,target,input_json,params_json,before_json,stripe_key,created_at,status) "
                    "VALUES (?, 'refund', ?, '{}', '{}', '{}', ?, 0, 'unresolved')",
                    (f"pending-{index}", fixture.id, f"pending-key-{index}"))
    return DryRunPolicy(store, lambda payment_id: dict(payments[payment_id]))


def grade(turn, intent, decision):
    reference = turn.reference_intent.model_dump()
    actual = intent.model_dump()
    fields = {name: actual[name] == reference[name] for name in turn.scored_fields}
    expected = turn.expected_decision
    decision_ok = decision.get("action") == expected.action
    if expected.action == "propose_refund":
        decision_ok = decision_ok and all(decision.get(name) == getattr(expected, name)
                                         for name in ("payment_id", "amount_minor", "currency"))
    return {"fields": fields, "extraction_passed": all(fields.values()),
            "decision_passed": decision_ok, "reason_passed": decision.get("code") in expected.reason_codes}


def evaluate_case(case, *, arm="rules", extractor=None, checkpoint=None):
    started = perf_counter()
    rows = []
    events = []

    def progress():
        return {"id": case.id, "category": case.category, "status": "in_progress", "turns": rows,
                "model_events": events, "expected_turns": 1 + len(case.clarifications),
                "decision_passed": False, "passed": False}

    with TemporaryDirectory(prefix="payresolve-benchmark-") as directory:
        policy = None if arm == "wrapper" else fixture_policy(AnalysisStore(Path(directory) / "case.db"), case.payments)
        for index, turn in enumerate([case.initial, *case.clarifications]):
            # Follow the observed path; do not feed replies after a wrong terminal action.
            if index and rows[-1]["decision"]["action"] != "clarify":
                break
            if arm == "rules":
                intent = rules.extract(turn.message)
                decision = policy.assess(turn.message, intent)
            else:
                event = {"turn": index, "status": "started", "model": extractor.model,
                         "attempts": None, "usage": None, "latency_ms": None}
                events.append(event)
                if checkpoint:
                    checkpoint(progress())  # Durable marker BEFORE a potentially billable call.
                try:
                    if arm == "full":
                        extracted = extractor.extract(turn.message)
                        intent = Intent.model_validate(extracted["intent"])
                        decision = policy.assess(turn.message, intent)
                    else:
                        extracted = extractor.decide(turn.message, case.payments)
                        intent = Intent.model_validate(extracted["output"]["intent"])
                        decision = extracted["output"]["decision"] | {"dry_run": True, "executed": False}
                    event.update(status="completed", **{key: extracted.get(key) for key in
                                 ("model", "prompt_version", "attempts", "usage", "latency_ms")})
                except ExtractionError as error:
                    event.update(status="error", code=error.code, attempts=error.attempts, latency_ms=error.latency_ms)
                    return progress() | {"status": "error", "error": {"code": error.code},
                                         "latency_ms": round((perf_counter() - started) * 1000, 3)}
            rows.append({"turn": index, "intent": intent.model_dump(), "decision": decision,
                         "grades": grade(turn, intent, decision)})
            if checkpoint:
                checkpoint(progress())
    expected_turns = 1 + len(case.clarifications)
    path_complete = len(rows) == expected_turns
    decision_passed = path_complete and all(row["grades"]["decision_passed"] for row in rows)
    passed = decision_passed and all(row["grades"]["extraction_passed"] and row["grades"]["reason_passed"] for row in rows)
    return {"id": case.id, "category": case.category, "status": "completed", "turns": rows,
            "model_events": events,
            "expected_turns": expected_turns, "path_complete": path_complete,
            "decision_passed": decision_passed, "passed": passed,
            "latency_ms": round((perf_counter() - started) * 1000, 3)}


def connect(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "runs.sqlite3", timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, dataset_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS benchmark_results (
            run_id TEXT NOT NULL REFERENCES benchmark_runs(id), case_id TEXT NOT NULL,
            result_json TEXT NOT NULL, PRIMARY KEY(run_id, case_id)
        );
    """)
    return connection


def load(connection, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise DatasetError("invalid_run_id", "Use the run ID returned by benchmark run.")
    row = connection.execute("SELECT metadata_json,dataset_json FROM benchmark_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise DatasetError("run_not_found", "No benchmark run exists with that ID.")
    return json.loads(row[0]), parse_dataset(json.loads(row[1]))


def create_run(path, *, case_ids=(), against=(), arm="rules", model=None, directory=DEFAULT_DIRECTORY):
    if arm not in ("rules", "full", "wrapper"):
        raise DatasetError("invalid_arm", "Choose rules, full, or wrapper.")
    if arm != "rules" and (not isinstance(model, str) or not re.fullmatch(r"gemini-[A-Za-z0-9.-]+", model)):
        raise DatasetError("invalid_model", "Specify a Gemini model for model-based runs.")
    if arm == "rules" and model is not None:
        raise DatasetError("invalid_model", "The rules arm does not use a model.")
    validation = validate_dataset(path, against=against)
    raw, data = read_json(path)
    if hashlib.sha256(raw).hexdigest() != validation["dataset_sha256"]:
        raise DatasetError("dataset_changed", "Dataset changed during validation; retry.")
    dataset = parse_dataset(data)
    available = {case.id for case in dataset.cases}
    if len(case_ids) != len(set(case_ids)) or set(case_ids) - available:
        raise DatasetError("invalid_case_selection", "Case IDs must be unique and present in the dataset.")
    selected = [case.id for case in dataset.cases if not case_ids or case.id in case_ids]
    run_id = uuid4().hex
    metadata = {"run_id": run_id, "created_at": now(), "runner_version": RUNNER_VERSION,
                "arm": arm, "rules_version": rules.RULES_VERSION if arm == "rules" else None, "code_sha256": fingerprint(),
                "model": model, "fallback_enabled": False,
                "prompt_version": PROMPT_VERSION if arm == "full" else WRAPPER_PROMPT_VERSION if arm == "wrapper" else None,
                "transport": None if arm == "rules" else {"timeout_ms": 30000, "max_http_attempts": 3,
                                                          "thinking_level": "low", "max_output_tokens": 2048},
                "python_version": platform.python_version(), "dataset_id": dataset.dataset_id,
                "dataset_sha256": validation["dataset_sha256"], "split": dataset.split,
                "validation": validation, "selected_cases": selected, "model_calls": 0, "stripe_calls": 0,
                "scope": "pre_approval_decisions_only", "execution_evaluated": False}
    connection = connect(directory)
    try:
        with connection:
            connection.execute("INSERT INTO benchmark_runs VALUES (?,?,?)",
                               (run_id, json.dumps(metadata), json.dumps(data)))
    finally:
        connection.close()
    return run_id


def resume(run_id, *, limit=None, directory=DEFAULT_DIRECTORY):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise DatasetError("invalid_limit", "Batch limit must be a positive integer.")
    connection = connect(directory)
    try:
        metadata, dataset = load(connection, run_id)
        if metadata["code_sha256"] != fingerprint():
            raise DatasetError("run_version_mismatch", "Code or environment changed. Start a new run; saved results remain available.")
        if metadata["arm"] != "rules":
            return resume_model(connection, metadata, dataset, limit=limit, directory=directory)
        count = 0
        for case in dataset.cases:
            if case.id not in metadata["selected_cases"]:
                continue
            if limit is not None and count >= limit:
                break
            # Serialize competing resumes and commit each complete case atomically.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM benchmark_results WHERE run_id=? AND case_id=?", (run_id, case.id)).fetchone():
                    continue
                try:
                    result = evaluate_case(case)
                except Exception:
                    # Never persist exception text (it can contain inputs/credentials).
                    result = {"id": case.id, "category": case.category, "status": "error",
                              "error": {"code": "case_evaluation_failed"}, "passed": False,
                              "decision_passed": False, "turns": [], "expected_turns": 1 + len(case.clarifications)}
                result["recorded_at"] = now()
                connection.execute("INSERT INTO benchmark_results VALUES (?,?,?)", (run_id, case.id, json.dumps(result)))
                count += 1
    finally:
        connection.close()
    return report(run_id, directory=directory)


def resume_model(connection, metadata, dataset, *, limit, directory):
    run_id, arm = metadata["run_id"], metadata["arm"]
    # A small default batch bounds live work. Explicit --limit can expand it.
    limit = 2 if limit is None else limit
    extractor = None
    count = 0
    for case in dataset.cases:
        if case.id not in metadata["selected_cases"] or count >= limit:
            continue
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = [json.loads(row[0]) for row in connection.execute(
                "SELECT result_json FROM benchmark_results WHERE run_id=?", (run_id,))]
            if any(row["status"] == "in_progress" for row in saved):
                break  # Another process is working, or an interrupted call needs a new run.
            if any(row["id"] == case.id for row in saved):
                continue
            if extractor is None:
                settings = Settings.from_environment()
                factory = GeminiExtractor if arm == "full" else GeminiDecisionWrapper
                extractor = factory(settings.gemini_api_key, metadata["model"], forbidden_values=(settings.stripe_secret_key,))
                # Check all prospective input before storing a started marker or calling a model.
                for turn in [case.initial, *case.clarifications]:
                    extractor.validate(turn.message)
            placeholder = {"id": case.id, "category": case.category, "status": "in_progress",
                           "turns": [], "model_events": [], "expected_turns": 1 + len(case.clarifications),
                           "passed": False, "decision_passed": False, "recorded_at": now()}
            connection.execute("INSERT INTO benchmark_results VALUES (?,?,?)", (run_id, case.id, json.dumps(placeholder)))

        def checkpoint(result):
            with connection:
                connection.execute("UPDATE benchmark_results SET result_json=? WHERE run_id=? AND case_id=?",
                                   (json.dumps(result | {"recorded_at": now()}), run_id, case.id))

        try:
            result = evaluate_case(case, arm=arm, extractor=extractor, checkpoint=checkpoint)
        except Exception:
            previous = json.loads(connection.execute(
                "SELECT result_json FROM benchmark_results WHERE run_id=? AND case_id=?", (run_id, case.id)).fetchone()[0])
            result = previous | {"status": "error", "error": {"code": "case_evaluation_failed"}}
        checkpoint(result)
        count += 1
        if result["status"] == "error":
            break  # Preserve the failure and stop; no outage multiplied across a dataset.
    return report(run_id, directory=directory)


def report(run_id, *, directory=DEFAULT_DIRECTORY, details=False):
    connection = connect(directory)
    try:
        metadata, dataset = load(connection, run_id)
        by_id = {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT case_id,result_json FROM benchmark_results WHERE run_id=?", (run_id,))}
    finally:
        connection.close()
    rows = [by_id[key] for key in metadata["selected_cases"] if key in by_id]
    scheduled = len(metadata["selected_cases"])
    completed = sum(row["status"] == "completed" for row in rows)
    passed = sum(row["passed"] for row in rows)
    in_progress = sum(row["status"] == "in_progress" for row in rows)
    events = [event for row in rows for event in row.get("model_events", [])]
    valid_outputs = sum(event["status"] == "completed" for event in events)
    completed_responses = sum(event["status"] == "completed" or event.get("code") == "invalid_model_output" for event in events)
    known_tokens = [event["usage"].get("total_tokens") for event in events if event.get("usage")]
    decisions = sum(row["decision_passed"] for row in rows)
    fields = [value for row in rows for turn in row["turns"] for value in turn["grades"]["fields"].values()]
    selected_cases = [case for case in dataset.cases if case.id in metadata["selected_cases"]]
    fields_scheduled = sum(len(turn.scored_fields) for case in selected_cases
                           for turn in [case.initial, *case.clarifications])
    clarification_cases = [case for case in selected_cases if case.clarifications]
    clarification_correct = sum(case.id in by_id and by_id[case.id]["decision_passed"] for case in clarification_cases)
    categories = {}
    for case in dataset.cases:
        if case.id in metadata["selected_cases"]:
            category = categories.setdefault(case.category, Counter(scheduled=0, completed=0, passed=0))
            category["scheduled"] += 1
            category["completed"] += case.id in by_id and by_id[case.id]["status"] == "completed"
            category["passed"] += case.id in by_id and by_id[case.id]["passed"]
    result = metadata | {"status": "completed" if completed == scheduled else "incomplete",
                         "scheduled": scheduled, "attempted": len(rows), "completed": completed,
                         "errors": len(rows) - completed - in_progress, "in_progress": in_progress,
                         "unattempted": scheduled - len(rows),
                         "model_calls": len(events), "valid_model_outputs": valid_outputs,
                         "completed_model_responses": completed_responses,
                         "model_response_completion_rate": completed_responses / len(events) if events else None,
                         "http_attempts_known": sum(event.get("attempts") or 0 for event in events),
                         "calls_with_unknown_attempts": sum(event.get("attempts") is None for event in events),
                         "reported_total_tokens": sum(value for value in known_tokens if value is not None),
                         "calls_with_unknown_token_usage": len(events) - sum(value is not None for value in known_tokens),
                         "passed": passed, "requested_case_pass_rate": passed / scheduled,
                         "decision_correct_cases": decisions,
                         "decision_accuracy_completed_cases": decisions / completed if completed else None,
                         "scored_fields_observed": len(fields), "correct_fields": sum(fields),
                         "scored_fields_scheduled": fields_scheduled,
                         "field_accuracy_observed": sum(fields) / len(fields) if fields else None,
                         "clarification_cases_scheduled": len(clarification_cases),
                         "clarification_paths_correct": clarification_correct,
                         "by_category": categories,
                         "database_path": str(Path(directory).resolve() / "runs.sqlite3")}
    if details:
        result["cases"] = rows
    return result


def compare(run_ids, *, directory=DEFAULT_DIRECTORY):
    if not 2 <= len(run_ids) <= 3 or len(set(run_ids)) != len(run_ids):
        raise DatasetError("invalid_comparison", "Supply two or three distinct runs, one per arm.")
    reports = [report(run_id, directory=directory, details=True) for run_id in run_ids]
    first = reports[0]
    if (len({item["arm"] for item in reports}) != len(reports)
            or any(item[key] != first[key] for item in reports for key in
                   ("dataset_sha256", "selected_cases", "code_sha256"))
            or len({item["model"] for item in reports if item["arm"] != "rules"}) > 1):
        raise DatasetError("incomparable_runs", "Use distinct arms with identical dataset, case selection, code fingerprint, and model for model arms.")
    metrics = ("run_id", "arm", "model", "status", "scheduled", "completed", "errors", "in_progress",
               "unattempted", "passed", "requested_case_pass_rate", "decision_correct_cases",
               "scored_fields_observed", "correct_fields", "model_calls", "http_attempts_known",
               "reported_total_tokens", "calls_with_unknown_token_usage", "clarification_paths_correct")
    return {"status": "completed" if all(item["status"] == "completed" for item in reports) else "incomplete",
            "scope": "pre_approval_decisions_only", "dataset_sha256": first["dataset_sha256"],
            "split": first["split"], "selected_cases": first["selected_cases"],
            "runs": [{key: item.get(key) for key in metrics} for item in reports],
            "case_passes": {case_id: {item["arm"]: next((row["passed"] for row in item["cases"] if row["id"] == case_id), None)
                                      for item in reports} for case_id in first["selected_cases"]},
            "note": "Decision comparison only. Draft/development cases do not establish held-out model superiority or execution safety."}
