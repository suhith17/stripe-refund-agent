from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.__main__ import main
from payresolve.benchmark import BenchmarkDataset, DatasetError, parse_dataset, read_json, validate_dataset
from payresolve.config import DEFAULT_GEMINI_MODEL, PROJECT_ROOT, Settings

PILOT = PROJECT_ROOT / "evals" / "benchmark" / "pilot.json"
LEGACY = PROJECT_ROOT / "evals" / "refund_intents.json"


class ModelDefaultsTests(unittest.TestCase):
    def test_default_is_flash_lite_without_fallback(self):
        with patch.dict("os.environ", {}, clear=True), patch("payresolve.config.dotenv_values", return_value={}):
            settings = Settings.from_environment()
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-3.5-flash-lite")
        self.assertEqual(settings.gemini_model, DEFAULT_GEMINI_MODEL)
        self.assertEqual(settings.gemini_fallback_model, "")
        self.assertEqual(Settings(Path("unused.db")).gemini_model, DEFAULT_GEMINI_MODEL)

    def test_explicit_environment_model_still_overrides_file(self):
        with patch.dict("os.environ", {"GEMINI_MODEL": "gemini-3.8-flash"}, clear=True), \
                patch("payresolve.config.dotenv_values", return_value={"GEMINI_MODEL": DEFAULT_GEMINI_MODEL}):
            self.assertEqual(Settings.from_environment().gemini_model, "gemini-3.8-flash")


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(PILOT.read_text(encoding="utf-8"))
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, data, name="dataset.json"):
        path = self.directory / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_pilot_contract_and_exported_schema(self):
        result = validate_dataset(PILOT, against=[LEGACY])
        self.assertEqual(result["cases"], 7)
        self.assertEqual(result["turns"], 9)
        self.assertEqual(result["draft_cases"], 7)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["stripe_calls"], 0)
        self.assertEqual(sum(result["near_duplicate_pairs"].values()), 0)
        self.assertEqual(json.loads(PILOT.with_name("schema.json").read_text()), BenchmarkDataset.model_json_schema())

    def test_invalid_fixture_balances_and_types(self):
        for changes in ({"refunded_minor": 2600}, {"captured_minor": 2600},
                        {"amount_minor": True}, {"amount_minor": 2500.0},
                        {"paid": False}, {"captured_minor": 0}, {"disputed": "false"}):
            data = deepcopy(self.data)
            data["cases"][0]["payments"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DatasetError):
                parse_dataset(data)

    def test_proposal_labels_require_all_valid_action_parameters(self):
        for changes in ({"amount_minor": 2600}, {"amount_minor": 700}, {"payment_id": "pi_missing"},
                        {"currency": "eur"}, {"amount_minor": None}, {"amount_minor": True}):
            data = deepcopy(self.data)
            data["cases"][0]["initial"]["expected_decision"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DatasetError):
                parse_dataset(data)
        self.data["cases"][1]["initial"]["expected_decision"]["amount_minor"] = 2000
        with self.assertRaises(DatasetError):
            parse_dataset(self.data)

    def test_ineligible_fixture_cannot_receive_proposal_label(self):
        for changes in ({"managed": False}, {"identity_matches": False}, {"disputed": True},
                        {"unresolved_operation": True}, {"livemode": True}, {"status": "processing"}):
            data = deepcopy(self.data)
            data["cases"][0]["payments"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DatasetError):
                parse_dataset(data)

    def test_gold_labels_require_grounded_fields_and_valid_clarification_order(self):
        for field, value in (("payment_ids", ["pi_invented"]), ("amount_evidence", "USD 99")):
            data = deepcopy(self.data)
            data["cases"][0]["initial"]["reference_intent"][field] = value
            with self.subTest(field=field), self.assertRaises(DatasetError):
                parse_dataset(data)
        self.data["cases"][0]["clarifications"] = self.data["cases"][2]["clarifications"]
        with self.assertRaises(DatasetError):
            parse_dataset(self.data)

    def test_duplicate_ids_normalized_messages_and_json_keys_rejected(self):
        data = deepcopy(self.data)
        data["cases"][1]["id"] = data["cases"][0]["id"]
        with self.assertRaises(DatasetError) as caught:
            parse_dataset(data)
        self.assertEqual(caught.exception.code, "duplicate_case_id")
        duplicate = deepcopy(self.data["cases"][0])
        duplicate["id"] = "duplicate"
        duplicate["initial"]["message"] = "  " + duplicate["initial"]["message"] + "  "
        self.data["cases"].append(duplicate)
        with self.assertRaises(DatasetError) as caught:
            validate_dataset(self.write(self.data))
        self.assertEqual(caught.exception.code, "duplicate_message")
        path = self.directory / "duplicate-keys.json"
        path.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaises(DatasetError) as caught:
            read_json(path)
        self.assertEqual(caught.exception.code, "duplicate_json_key")

    def test_reference_overlap_and_family_leakage_rejected(self):
        legacy = self.write([{"message": self.data["cases"][0]["initial"]["message"]}], "reference.json")
        with self.assertRaises(DatasetError) as caught:
            validate_dataset(PILOT, against=[legacy])
        self.assertEqual(caught.exception.code, "message_overlap")
        with self.assertRaises(DatasetError) as caught:
            validate_dataset(PILOT, against=[PILOT])
        self.assertEqual(caught.exception.code, "family_overlap")

    def test_near_duplicates_reported_and_draft_cannot_be_final(self):
        duplicate = deepcopy(self.data["cases"][0])
        duplicate["id"] = "near_duplicate"
        duplicate["initial"]["message"] = "Hi! " + duplicate["initial"]["message"]
        self.data["cases"].append(duplicate)
        result = validate_dataset(self.write(self.data))
        self.assertGreater(result["near_duplicate_pairs"]["within_dataset"], 0)
        with self.assertRaises(DatasetError) as caught:
            validate_dataset(PILOT, against=[LEGACY], require_final=True)
        self.assertEqual(caught.exception.code, "not_ready_to_freeze")

    def test_credentials_malformed_files_and_boolean_version_rejected(self):
        path = self.directory / "invalid.json"
        for text, code in [('not JSON', 'dataset_unreadable'), ('"sk_test_fakecredential"', 'credential_in_dataset'),
                           ('"sk_\\u0074est_fakecredential"', 'credential_in_dataset')]:
            path.write_text(text)
            with self.subTest(code=code), self.assertRaises(DatasetError) as caught:
                read_json(path)
            self.assertEqual(caught.exception.code, code)
        self.data["schema_version"] = True
        with self.assertRaises(DatasetError):
            parse_dataset(self.data)

    def test_cli_uses_no_settings_or_model_and_errors_do_not_echo_input(self):
        output = io.StringIO()
        with patch("sys.argv", ["payresolve", "benchmark", "validate", str(PILOT)]), \
                patch("payresolve.__main__.Settings.from_environment", side_effect=AssertionError("must stay offline")), \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["cases"], 7)
        self.data["private unexpected input"] = "never echo this value"
        output = io.StringIO()
        with patch("sys.argv", ["payresolve", "benchmark", "validate", str(self.write(self.data))]), redirect_stderr(output):
            self.assertEqual(main(), 1)
        self.assertNotIn("never echo", output.getvalue())
        self.assertNotIn("private unexpected", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "dataset_invalid")


if __name__ == "__main__":
    unittest.main()
