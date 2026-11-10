from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.benchmark import parse_dataset
from payresolve.benchmark_runner import connect, create_run
from payresolve.benchmark_simulator import Ledger, simulate_case, simulate_run
from payresolve.config import PROJECT_ROOT

PILOT = PROJECT_ROOT / "evals" / "benchmark" / "pilot.json"


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.dataset = parse_dataset(json.loads(PILOT.read_text()))

    def saved(self, case):
        rows = []
        for index, turn in enumerate([case.initial, *case.clarifications]):
            expected = turn.expected_decision
            rows.append({"turn": index, "decision": {"action": expected.action,
                         "payment_id": expected.payment_id, "amount_minor": expected.amount_minor,
                         "currency": expected.currency}})
        return {"id": case.id, "status": "completed", "turns": rows}

    def test_full_remaining_balance_uses_actual_ledger_and_prior_refunds(self):
        case = self.dataset.cases[1]
        result = simulate_case(case, self.saved(case))
        self.assertEqual(result["effects"], 1)
        self.assertEqual(result["ledger"]["refunded_balances"][case.payments[0].id], 3000)
        self.assertTrue(result["simulation_passed"])

    def test_provider_rejection_does_not_hide_inappropriate_attempt(self):
        case = self.dataset.cases[0]
        saved = self.saved(case)
        saved["turns"][0]["decision"]["amount_minor"] = 99999
        result = simulate_case(case, saved)
        self.assertEqual((result["refund_attempts"], result["inappropriate_attempts"], result["effects"]), (1, 1, 0))
        self.assertEqual(result["attempts"][0]["provider_result"]["code"], "insufficient_remaining")
        self.assertFalse(result["simulation_passed"])

    def test_wrong_target_creates_wrong_effect_even_with_same_total(self):
        case = deepcopy(self.dataset.cases[0])
        extra = deepcopy(case.payments[0])
        extra.id = "pi_other"
        case.payments.append(extra)
        saved = self.saved(case)
        saved["turns"][0]["decision"]["payment_id"] = extra.id
        result = simulate_case(case, saved)
        self.assertEqual((result["inappropriate_attempts"], result["incorrect_effects"], result["missing_effects"]), (1, 1, 1))
        self.assertFalse(result["ledger_matches_reference"])

    def test_premature_refund_fails_despite_matching_final_balance(self):
        case = self.dataset.cases[2]
        saved = self.saved(case)
        saved["turns"] = [{"turn": 0, "decision": saved["turns"][1]["decision"]}]
        result = simulate_case(case, saved)
        self.assertEqual(result["ledger"]["refunded_balances"], result["expected_refunded_balances"])
        self.assertEqual(result["incorrect_effects"], 1)
        self.assertFalse(result["simulation_passed"])

    def test_simulator_does_not_apply_application_dispute_policy(self):
        case = self.dataset.cases[4]
        saved = self.saved(case)
        saved["turns"][0]["decision"] = dict(action="propose_refund", payment_id=case.payments[0].id,
                                            amount_minor=500, currency="usd", status="verified")
        result = simulate_case(case, saved)
        self.assertEqual((result["effects"], result["incorrect_effects"]), (1, 1))
        self.assertFalse(result["simulation_passed"])

    def test_idempotency_replay_and_conflict_leave_one_effect(self):
        ledger = Ledger(self.dataset.cases[0].payments)
        self.addCleanup(ledger.close)
        proposal = dict(payment_id="pi_pilotA", amount_minor=725, currency="usd")
        first = ledger.submit("same", 0, proposal)
        second = ledger.submit("same", 0, proposal)
        self.assertEqual(first["refund_id"], second["refund_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(ledger.submit("same", 0, proposal | {"amount_minor": 100})["code"], "idempotency_conflict")
        self.assertEqual(len(ledger.snapshot()["effects"]), 1)

    def test_invalid_types_and_currency_do_not_change_ledger(self):
        ledger = Ledger(self.dataset.cases[0].payments)
        self.addCleanup(ledger.close)
        for amount, currency in ((True, "usd"), (-1, "usd"), (1.5, "usd"), (100, "eur")):
            result = ledger.submit("attempt", 0, dict(payment_id="pi_pilotA", amount_minor=amount, currency=currency))
            self.assertEqual(result["status"], "rejected")
        self.assertEqual(ledger.snapshot()["effects"], [])

    def test_saved_run_replay_is_offline_preserves_source_and_marks_skipped(self):
        with TemporaryDirectory() as directory:
            first, second = self.dataset.cases[:2]
            run_id = create_run(PILOT, case_ids=[first.id, second.id], directory=directory)
            saved = json.dumps(self.saved(first))
            connection = connect(directory)
            with connection:
                connection.execute("INSERT INTO benchmark_results VALUES (?,?,?)", (run_id, first.id, saved))
            connection.close()
            with patch("payresolve.config.Settings.from_environment", side_effect=AssertionError("must stay offline")):
                result = simulate_run(run_id, directory=directory)
            self.assertEqual((result["scheduled"], result["simulated"], result["passed"]), (2, 1, 1))
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual((result["model_calls"], result["stripe_calls"]), (0, 0))
            artifact = json.loads(Path(result["report_path"]).read_text())
            self.assertTrue(artifact["cases"][0]["ledger_matches_reference"])
            connection = connect(directory)
            self.assertEqual(connection.execute("SELECT result_json FROM benchmark_results WHERE run_id=?", (run_id,)).fetchone()[0], saved)
            connection.close()


if __name__ == "__main__":
    unittest.main()
