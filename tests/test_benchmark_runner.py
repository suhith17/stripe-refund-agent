from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve import benchmark_runner as runner
from payresolve.benchmark import DatasetError, parse_dataset
from payresolve.config import PROJECT_ROOT
from payresolve.rules import extract
from payresolve.__main__ import main

PILOT = PROJECT_ROOT / "evals" / "benchmark" / "pilot.json"


class RulesTests(unittest.TestCase):
    def test_numeric_evidence_and_original_price(self):
        value = extract("I paid USD 90 on pi_alpha. Please refund USD 12.50.")
        self.assertEqual(value.amount, "12.50")
        self.assertEqual(value.amount_evidence, "USD 12.50")
        self.assertEqual(value.currency, "usd")
        self.assertEqual(value.payment_ids, ["pi_alpha"])

    def test_negation_conflicts_and_unsupported_amounts(self):
        self.assertEqual(extract("Do not refund pi_alpha").intent, "other")
        self.assertTrue(extract("Refund USD 2 or USD 3 from pi_alpha").ambiguous)
        self.assertTrue(extract("If it arrives late, refund $2 from pi_alpha").ambiguous)
        self.assertIsNone(extract("Refund 50 cents from pi_alpha").amount)
        self.assertIsNone(extract("Refund $2.345 from pi_alpha").amount)
        self.assertEqual(extract("Refund -2 USD from pi_alpha").amount, "-2")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.data = json.loads(PILOT.read_text(encoding="utf-8"))
        self.dataset = parse_dataset(self.data)

    def start(self, **kwargs):
        return runner.create_run(PILOT, directory=self.directory, **kwargs)

    def test_batch_resume_and_repeated_resume_do_not_repeat_cases(self):
        run_id = self.start()
        result = runner.resume(run_id, limit=2, directory=self.directory)
        self.assertEqual((result["scheduled"], result["attempted"], result["unattempted"]), (7, 2, 5))
        self.assertEqual(result["status"], "incomplete")
        with patch.object(runner, "evaluate_case", wraps=runner.evaluate_case) as evaluate:
            result = runner.resume(run_id, directory=self.directory)
            self.assertEqual(evaluate.call_count, 5)
        self.assertEqual(result["completed"], 7)
        with patch.object(runner, "evaluate_case", side_effect=AssertionError("must not repeat")):
            self.assertEqual(runner.resume(run_id, directory=self.directory), result)

    def test_single_case_and_snapshot_survive_source_change(self):
        path = self.directory / "source.json"
        path.write_text(json.dumps(self.data))
        case_id = self.dataset.cases[2].id
        run_id = runner.create_run(path, case_ids=[case_id], directory=self.directory)
        path.write_text("broken input")
        result = runner.resume(run_id, directory=self.directory)
        self.assertEqual((result["scheduled"], result["completed"]), (1, 1))
        details = runner.report(run_id, directory=self.directory, details=True)
        self.assertEqual(len(details["cases"][0]["turns"]), 2)
        self.assertTrue(details["cases"][0]["path_complete"])

    def test_errors_are_durable_sanitized_and_not_silently_retried(self):
        run_id = self.start(case_ids=[self.dataset.cases[0].id])
        with patch.object(runner, "evaluate_case", side_effect=ValueError("private exception text")):
            result = runner.resume(run_id, directory=self.directory)
        self.assertEqual((result["errors"], result["completed"], result["unattempted"]), (1, 0, 0))
        with patch.object(runner, "evaluate_case", side_effect=AssertionError("must not retry")):
            runner.resume(run_id, directory=self.directory)
        self.assertNotIn("private exception", json.dumps(runner.report(run_id, directory=self.directory, details=True)))

    def test_interruption_rolls_back_only_current_case(self):
        run_id = self.start()
        evaluate = runner.evaluate_case
        calls = 0

        def interrupt(case):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return evaluate(case)

        with patch.object(runner, "evaluate_case", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            runner.resume(run_id, directory=self.directory)
        self.assertEqual(runner.report(run_id, directory=self.directory)["attempted"], 1)
        self.assertEqual(runner.resume(run_id, directory=self.directory)["completed"], 7)

    def test_version_mismatch_and_invalid_selection(self):
        run_id = self.start()
        with patch.object(runner, "fingerprint", return_value="changed"), self.assertRaises(DatasetError) as caught:
            runner.resume(run_id, directory=self.directory)
        self.assertEqual(caught.exception.code, "run_version_mismatch")
        self.assertEqual(runner.report(run_id, directory=self.directory)["attempted"], 0)
        with self.assertRaises(DatasetError):
            self.start(case_ids=["absent"])
        with self.assertRaises(DatasetError):
            runner.resume(run_id, limit=0, directory=self.directory)

    def test_concurrent_resumes_checkpoint_each_case_once(self):
        run_id = self.start()
        with patch.object(runner, "evaluate_case", wraps=runner.evaluate_case) as evaluate:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(runner.resume, run_id, directory=self.directory) for _ in range(2)]
                for future in futures:
                    future.result()
            self.assertEqual(evaluate.call_count, 7)
        self.assertEqual(runner.report(run_id, directory=self.directory)["attempted"], 7)

    def test_cli_remains_offline_and_exit_codes_distinguish_incomplete_and_failed(self):
        for result, code in ((dict(errors=0, unattempted=2, passed=1, scheduled=3), 2),
                             (dict(errors=1, unattempted=0, passed=0, scheduled=1), 1),
                             (dict(errors=0, unattempted=0, passed=0, scheduled=1), 1),
                             (dict(errors=0, unattempted=0, passed=1, scheduled=1), 0)):
            with self.subTest(code=code), \
                    patch("sys.argv", ["payresolve", "benchmark", "resume", "a" * 32]), \
                    patch("payresolve.__main__.Settings.from_environment", side_effect=AssertionError("no .env")), \
                    patch.object(runner, "resume", return_value=result), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), code)

    def test_grader_requires_parameters_and_reports_reason_separately(self):
        turn = self.dataset.cases[0].initial
        decision = dict(action="propose_refund", code="wrong_reason", payment_id="pi_wrong", amount_minor=725, currency="usd")
        result = runner.grade(turn, turn.reference_intent, decision)
        self.assertFalse(result["decision_passed"])
        decision["payment_id"] = turn.expected_decision.payment_id
        result = runner.grade(turn, turn.reference_intent, decision)
        self.assertTrue(result["decision_passed"])
        self.assertFalse(result["reason_passed"])

    def test_labels_do_not_reach_extractor_and_wrong_path_is_not_corrected(self):
        case = deepcopy(self.dataset.cases[2])
        with patch.object(runner.rules, "extract", return_value=extract("No refund")) as extractor:
            row = runner.evaluate_case(case)
        extractor.assert_called_once_with(case.initial.message)
        self.assertEqual(len(row["turns"]), 1)
        self.assertFalse(row["path_complete"])
        self.assertFalse(row["passed"])

    def test_fixture_flags_reach_real_policy_without_network(self):
        for flag, value, code in (("managed", False, "unmanaged_payment"),
                                  ("identity_matches", False, "payment_identity_mismatch"),
                                  ("unresolved_operation", True, "unresolved_operation"),
                                  ("livemode", True, "payment_identity_mismatch"),
                                  ("disputed", True, "payment_disputed")):
            case = deepcopy(self.dataset.cases[1])
            setattr(case.payments[0], flag, value)
            with self.subTest(flag=flag):
                row = runner.evaluate_case(case)
                self.assertEqual(row["turns"][0]["decision"]["code"], code)


if __name__ == "__main__":
    unittest.main()
