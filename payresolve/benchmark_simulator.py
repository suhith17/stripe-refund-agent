"""Offline replay of saved decisions against an independent synthetic ledger."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .benchmark import DatasetError

SIMULATOR_VERSION = "saved-decision-ledger-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Ledger:
    """Simplified provider constraints, not a Stripe emulator or application policy."""

    def __init__(self, fixtures):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE payments (id TEXT PRIMARY KEY, currency TEXT NOT NULL,
                captured INTEGER NOT NULL, initial_refunded INTEGER NOT NULL, refundable INTEGER NOT NULL);
            CREATE TABLE refunds (id INTEGER PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL,
                params_json TEXT NOT NULL, turn_index INTEGER NOT NULL,
                payment_id TEXT NOT NULL REFERENCES payments(id), amount_minor INTEGER NOT NULL);
        """)
        with self.connection:
            self.connection.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", [
                (p.id, p.currency, p.captured_minor, p.refunded_minor,
                 int(p.paid and p.captured and p.status == "succeeded" and not p.livemode)) for p in fixtures])

    def close(self):
        self.connection.close()

    def submit(self, key, turn, proposal):
        params = {name: proposal.get(name) for name in ("payment_id", "amount_minor", "currency")}
        payment_id, amount, currency = (params[name] for name in ("payment_id", "amount_minor", "currency"))
        if not isinstance(payment_id, str) or type(amount) is not int or amount <= 0 or not isinstance(currency, str):
            return {"status": "rejected", "code": "invalid_parameters", "replayed": False}
        with self.connection:
            existing = self.connection.execute("SELECT * FROM refunds WHERE operation_key=?", (key,)).fetchone()
            if existing:
                if existing["params_json"] != canonical(params):
                    return {"status": "rejected", "code": "idempotency_conflict", "replayed": False}
                return {"status": "succeeded", "refund_id": existing["id"], "replayed": True}
            payment = self.connection.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            if payment is None:
                return {"status": "rejected", "code": "payment_not_found", "replayed": False}
            if not payment["refundable"] or currency != payment["currency"]:
                return {"status": "rejected", "code": "payment_not_refundable", "replayed": False}
            used = self.connection.execute("SELECT COALESCE(SUM(amount_minor),0) FROM refunds WHERE payment_id=?", (payment_id,)).fetchone()[0]
            if amount > payment["captured"] - payment["initial_refunded"] - used:
                return {"status": "rejected", "code": "insufficient_remaining", "replayed": False}
            cursor = self.connection.execute(
                "INSERT INTO refunds (operation_key,params_json,turn_index,payment_id,amount_minor) VALUES (?,?,?,?,?)",
                (key, canonical(params), turn, payment_id, amount))
            return {"status": "succeeded", "refund_id": cursor.lastrowid, "replayed": False}

    def snapshot(self):
        # Read the ledger independently of submit's reported result.
        effects = [dict(row) for row in self.connection.execute(
            "SELECT id,operation_key,turn_index,payment_id,amount_minor FROM refunds ORDER BY id")]
        balances = {row["id"]: row["initial_refunded"] + sum(effect["amount_minor"] for effect in effects if effect["payment_id"] == row["id"])
                    for row in self.connection.execute("SELECT * FROM payments ORDER BY id")}
        return {"effects": effects, "refunded_balances": balances}


def simulate_case(case, saved):
    references = [case.initial, *case.clarifications]
    rows = saved["turns"]
    indices = [row["turn"] for row in rows]
    if indices != list(range(len(indices))) or len(indices) > len(references):
        raise DatasetError("invalid_saved_trace", "Saved turns must be an ordered prefix of the case.")
    expected = Counter((index, turn.expected_decision.payment_id, turn.expected_decision.amount_minor)
                       for index, turn in enumerate(references) if turn.expected_decision.action == "propose_refund")
    expected_balances = {payment.id: payment.refunded_minor for payment in case.payments}
    for (_, payment_id, amount), count in expected.items():
        expected_balances[payment_id] += amount * count
    ledger = Ledger(case.payments)
    attempts = []
    decisions_match = len(rows) == len(references)
    try:
        for row in rows:
            index, decision = row["turn"], row["decision"]
            label = references[index].expected_decision
            matches = decision.get("action") == label.action
            if label.action == "propose_refund":
                matches = matches and all(decision.get(name) == getattr(label, name)
                                          for name in ("payment_id", "amount_minor", "currency"))
            decisions_match = decisions_match and matches
            if decision.get("action") == "propose_refund":
                result = ledger.submit(f"{case.id}-turn-{index}", index, decision)
                attempts.append({"turn": index, "appropriate": matches,
                                 "proposal": {name: decision.get(name) for name in ("payment_id", "amount_minor", "currency")},
                                 "provider_result": result})
        snapshot = ledger.snapshot()
    finally:
        ledger.close()
    actual = Counter((effect["turn_index"], effect["payment_id"], effect["amount_minor"]) for effect in snapshot["effects"])
    ledger_matches = actual == expected and snapshot["refunded_balances"] == expected_balances
    return {"id": case.id, "category": case.category, "attempts": attempts,
            "refund_attempts": len(attempts), "inappropriate_attempts": sum(not item["appropriate"] for item in attempts),
            "effects": len(snapshot["effects"]), "incorrect_effects": sum((actual - expected).values()),
            "missing_effects": sum((expected - actual).values()),
            "ledger": snapshot, "expected_refunded_balances": expected_balances,
            "expected_effects": [{"turn": turn, "payment_id": payment_id, "amount_minor": amount, "count": count}
                                 for (turn, payment_id, amount), count in expected.items()],
            "decision_path_matches_reference": decisions_match, "ledger_matches_reference": ledger_matches,
            "simulation_passed": decisions_match and ledger_matches}


def simulate_run(run_id, *, directory=None):
    from . import benchmark_runner as runner
    directory = Path(directory or runner.DEFAULT_DIRECTORY)
    connection = runner.connect(directory)
    try:
        metadata, dataset = runner.load(connection, run_id)
        saved = {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT case_id,result_json FROM benchmark_results WHERE run_id=?", (run_id,))}
    finally:
        connection.close()
    source = {"metadata": metadata, "dataset": dataset.model_dump(), "cases": saved}
    cases, skipped = [], []
    for case in dataset.cases:
        if case.id not in metadata["selected_cases"]:
            continue
        row = saved.get(case.id)
        if row is None or row["status"] != "completed":
            skipped.append({"id": case.id, "source_status": row["status"] if row else "unattempted"})
        else:
            cases.append(simulate_case(case, row))
    result = {"simulation_id": uuid4().hex, "created_at": runner.now(), "source_run_id": run_id,
              "source_arm": metadata["arm"], "source_model": metadata.get("model"),
              "source_code_sha256": metadata["code_sha256"],
              "source_snapshot_sha256": hashlib.sha256(canonical(source).encode()).hexdigest(),
              "dataset_sha256": metadata["dataset_sha256"], "split": dataset.split,
              "simulator_version": SIMULATOR_VERSION,
              "simulator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scope": "offline_saved_decision_replay", "status": "incomplete" if skipped else "completed",
              "scheduled": len(metadata["selected_cases"]), "simulated": len(cases), "skipped": skipped,
              "passed": sum(case["simulation_passed"] for case in cases),
              "refund_attempts": sum(case["refund_attempts"] for case in cases),
              "inappropriate_attempts": sum(case["inappropriate_attempts"] for case in cases),
              "effects": sum(case["effects"] for case in cases),
              "incorrect_effects": sum(case["incorrect_effects"] for case in cases),
              "missing_effects": sum(case["missing_effects"] for case in cases),
              "model_calls": 0, "stripe_calls": 0, "cases": cases,
              "note": "Simulated effects only. Independent ledger checks do not verify the application's real execution/recovery workflow."}
    output_directory = directory / "simulations"
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"{result['simulation_id']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return {key: value for key, value in result.items() if key != "cases"} | {"report_path": str(path.resolve())}
