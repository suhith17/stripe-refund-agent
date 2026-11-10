"""Offline dataset contract and integrity checks; not a model evaluation runner."""

from collections import Counter
from decimal import Decimal
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .intent import Intent, PAYMENT_ID

CATEGORY_TARGETS = {"eligible_partial": 20, "eligible_full": 15, "missing_information": 20,
                    "ambiguous": 15, "ineligible_payment": 20, "invalid_amount": 15,
                    "unrelated_adversarial": 15}
Category = Literal["eligible_partial", "eligible_full", "missing_information", "ambiguous",
                   "ineligible_payment", "invalid_amount", "unrelated_adversarial"]
SAFE_ID = r"^[a-z][a-z0-9_-]{0,79}$"
NEAR_DUPLICATE_THRESHOLD = 0.92


class DatasetError(Exception):
    def __init__(self, code, message, issues=None):
        super().__init__(message)
        self.code, self.message, self.issues = code, message, issues or []


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PaymentFixture(StrictRecord):
    id: str = Field(pattern=r"^pi_[A-Za-z0-9]+$")
    amount_minor: int = Field(ge=0)
    captured_minor: int = Field(ge=0)
    refunded_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[a-z]{3}$")
    status: Literal["succeeded", "requires_payment_method", "requires_capture", "canceled", "processing"]
    paid: bool
    captured: bool
    disputed: bool
    managed: bool
    identity_matches: bool
    unresolved_operation: bool
    livemode: bool

    @model_validator(mode="after")
    def balances(self):
        if not 0 <= self.refunded_minor <= self.captured_minor <= self.amount_minor:
            raise ValueError("Fixture balances must satisfy refunded <= captured <= amount.")
        if not self.paid and self.captured_minor:
            raise ValueError("An unpaid fixture cannot have captured funds.")
        if self.captured and (not self.paid or not self.captured_minor):
            raise ValueError("A captured fixture must be paid with a positive captured amount.")
        return self


class ExpectedDecision(StrictRecord):
    action: Literal["propose_refund", "clarify", "reject", "no_action"]
    reason_codes: list[str] = Field(min_length=1, max_length=10)
    payment_id: str | None = None
    amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = None

    @model_validator(mode="after")
    def parameters(self):
        values = (self.payment_id, self.amount_minor, self.currency)
        if self.action == "propose_refund":
            if any(value is None for value in values) or self.currency != "usd":
                raise ValueError("A proposal label requires a payment, positive integer amount, and usd currency.")
        elif any(value is not None for value in values):
            raise ValueError("A non-proposal label cannot contain refund parameters.")
        if len(set(self.reason_codes)) != len(self.reason_codes) or any(
                not re.fullmatch(SAFE_ID, code) for code in self.reason_codes):
            raise ValueError("Reason codes must be unique lowercase identifiers.")
        return self


class LabeledTurn(StrictRecord):
    message: str = Field(min_length=1, max_length=4000)
    reference_intent: Intent
    scored_fields: list[str] = Field(min_length=1, max_length=10)
    expected_decision: ExpectedDecision

    @model_validator(mode="after")
    def grounding(self):
        if not self.message.strip() or "\x00" in self.message:
            raise ValueError("Message must contain text without null bytes.")
        if (len(self.reference_intent.payment_ids) != len(set(self.reference_intent.payment_ids))
                or set(self.reference_intent.payment_ids) != set(PAYMENT_ID.findall(self.message))):
            raise ValueError("Reference payment IDs must match the IDs literally present in the message.")
        evidence = self.reference_intent.amount_evidence
        if evidence is not None and evidence not in self.message:
            raise ValueError("Reference amount evidence must occur in the message.")
        if len(set(self.scored_fields)) != len(self.scored_fields) or not set(self.scored_fields) <= Intent.model_fields.keys():
            raise ValueError("Scored extraction fields must be unique members of the intent schema.")
        return self


class BenchmarkCase(StrictRecord):
    id: str = Field(pattern=SAFE_ID)
    family_id: str = Field(pattern=SAFE_ID)
    category: Category
    tags: list[str]
    review_status: Literal["draft", "reviewed"]
    rationale: str = Field(min_length=10, max_length=2000)
    payments: list[PaymentFixture] = Field(max_length=10)
    initial: LabeledTurn
    clarifications: list[LabeledTurn] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def coherence(self):
        payments = {payment.id: payment for payment in self.payments}
        if len(payments) != len(self.payments):
            raise ValueError("Fixture payment IDs must be unique within a case.")
        turns = [self.initial, *self.clarifications]
        for index, turn in enumerate(turns):
            if index and turns[index - 1].expected_decision.action != "clarify":
                raise ValueError("A clarification response must follow a clarify label.")
            expected, reference = turn.expected_decision, turn.reference_intent
            if expected.action != "propose_refund":
                continue
            payment = payments.get(expected.payment_id)
            if (payment is None or not payment.managed or not payment.identity_matches or payment.livemode
                    or payment.unresolved_operation or payment.disputed or payment.status != "succeeded"
                    or not payment.paid or not payment.captured or payment.currency != expected.currency):
                raise ValueError("Proposal label must target an eligible USD sandbox fixture.")
            if (reference.intent != "refund" or reference.ambiguous
                    or set(reference.payment_ids) != {expected.payment_id}):
                raise ValueError("Proposal label needs an unambiguous reference refund for its single target.")
            remaining = payment.captured_minor - payment.refunded_minor
            if expected.amount_minor > remaining:
                raise ValueError("Proposal label exceeds the remaining refundable amount.")
            if reference.refund_type == "full":
                if expected.amount_minor != remaining or reference.amount is not None:
                    raise ValueError("Full-refund label must equal the entire remaining amount.")
            elif reference.refund_type == "partial":
                if (reference.amount is None or not reference.amount_evidence or reference.currency != expected.currency
                        or Decimal(reference.amount) * 100 != expected.amount_minor):
                    raise ValueError("Partial-refund label must match the reference amount and currency.")
            else:
                raise ValueError("Proposal label needs a full or partial reference refund.")
        return self


class BenchmarkDataset(StrictRecord):
    schema_version: Literal[1]
    dataset_id: str = Field(pattern=SAFE_ID)
    split: Literal["development", "test"]
    author: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10, max_length=2000)
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def version_type(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("Schema version must be an integer.")
        return value


def normalized(message):
    return " ".join(unicodedata.normalize("NFKC", message).casefold().split())


def read_json(path):
    def unique_keys(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise DatasetError("duplicate_json_key", "JSON objects must not repeat field names.")
        return dict(pairs)

    try:
        raw = Path(path).read_bytes()
        if len(raw) > 5_000_000:
            raise DatasetError("dataset_too_large", "Dataset files must be at most 5 MB.")
        # Scan before validation errors can echo source text. Never print contents.
        if re.search(rb"(?:[spr]k_(?:test|live)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{20,})", raw):
            raise DatasetError("credential_in_dataset", "Remove credentials from the dataset.")
        data = json.loads(raw, object_pairs_hook=unique_keys)
        if re.search(r"(?:[spr]k_(?:test|live)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{20,})", json.dumps(data, ensure_ascii=False)):
            raise DatasetError("credential_in_dataset", "Remove credentials from the dataset.")
        return raw, data
    except (OSError, ValueError):
        raise DatasetError("dataset_unreadable", "Cannot read the dataset as JSON.") from None


def parse_dataset(data):
    try:
        dataset = BenchmarkDataset.model_validate(data)
    except ValidationError as error:
        fields = set().union(*(record.model_fields for record in
                             (BenchmarkDataset, BenchmarkCase, LabeledTurn, ExpectedDecision, PaymentFixture, Intent)))
        issues = [{"path": [part if isinstance(part, int) or part in fields else "<field>" for part in item["loc"]],
                   "type": item["type"]} for item in error.errors(include_input=False)[:20]]
        raise DatasetError("dataset_invalid", "Dataset violates its schema or fixture/label constraints.", issues) from None
    ids = [case.id for case in dataset.cases]
    if len(ids) != len(set(ids)):
        raise DatasetError("duplicate_case_id", "Case IDs must be unique.")
    return dataset


def turns(dataset):
    return [turn.message for case in dataset.cases for turn in [case.initial, *case.clarifications]]


def validate_dataset(path, *, against=(), require_final=False):
    raw, data = read_json(path)
    dataset = parse_dataset(data)
    messages = [normalized(message) for message in turns(dataset)]
    if len(messages) != len(set(messages)):
        raise DatasetError("duplicate_message", "Dataset contains duplicate normalized messages, including clarification replies.")
    families = {case.family_id for case in dataset.cases}
    references = []
    for other_path in against:
        other_raw, other_data = read_json(other_path)
        if isinstance(other_data, list):
            # The existing 20-case development dataset predates this contract.
            if not other_data or any(not isinstance(case, dict) or not isinstance(case.get("message"), str) for case in other_data):
                raise DatasetError("reference_invalid", "Legacy references must contain cases with message strings.")
            other_messages = [normalized(case["message"]) for case in other_data]
        else:
            other = parse_dataset(other_data)
            if families & {case.family_id for case in other.cases}:
                raise DatasetError("family_overlap", "Scenario families overlap a supplied reference dataset.")
            other_messages = [normalized(message) for message in turns(other)]
        if set(messages) & set(other_messages):
            raise DatasetError("message_overlap", "Messages overlap a supplied reference dataset.")
        references.append({"sha256": hashlib.sha256(other_raw).hexdigest(), "messages": other_messages})
    internal_near = sum(SequenceMatcher(None, a, b).ratio() >= NEAR_DUPLICATE_THRESHOLD
                        for index, a in enumerate(messages) for b in messages[index + 1:])
    reference_near = sum(SequenceMatcher(None, a, b).ratio() >= NEAR_DUPLICATE_THRESHOLD
                         for a in messages for reference in references for b in reference["messages"])
    counts = dict(Counter(case.category for case in dataset.cases))
    reviewed = sum(case.review_status == "reviewed" for case in dataset.cases)
    if require_final and (dataset.split != "test" or counts != CATEGORY_TARGETS
                          or reviewed != 120 or not references or internal_near or reference_near):
        raise DatasetError("not_ready_to_freeze", "Final readiness requires test split, the planned 120-case distribution, reviewed labels, supplied development references, and no unresolved near-duplicate flags.")
    return {"status": "valid", "dataset_id": dataset.dataset_id, "split": dataset.split,
            "schema_version": dataset.schema_version, "dataset_sha256": hashlib.sha256(raw).hexdigest(),
            "cases": len(dataset.cases), "turns": len(messages), "category_counts": counts,
            "reviewed_cases": reviewed, "draft_cases": len(dataset.cases) - reviewed,
            "reference_hashes": [item["sha256"] for item in references],
            "near_duplicate_pairs": {"within_dataset": internal_near, "against_references": reference_near},
            "final_readiness_checked": require_final,
            "note": "Structural validation only; labels still require human review. No model or Stripe evaluation was run.",
            "model_calls": 0, "stripe_calls": 0}


def add_parser(subparsers):
    parser = subparsers.add_parser("benchmark", help="Offline benchmark dataset checks")
    commands = parser.add_subparsers(dest="benchmark_command", required=True)
    validate = commands.add_parser("validate", help="Validate schema, fixtures, labels, and overlap without APIs")
    validate.add_argument("dataset")
    validate.add_argument("--against", action="append", default=[], help="Development/reference dataset; repeat for multiple files")
    validate.add_argument("--require-final", action="store_true", help="Require planned 120-case distribution and declared label review")
    run = commands.add_parser("run", help="Start a decision pilot (rules offline; full/wrapper call Gemini)")
    run.add_argument("dataset")
    run.add_argument("--arm", choices=("rules", "full", "wrapper"), default="rules")
    run.add_argument("--model", help="Explicit Gemini model; otherwise use the configured primary. No fallback.")
    run.add_argument("--case", action="append", default=[], dest="case_ids", help="Case ID to include; repeat to select multiple")
    run.add_argument("--against", action="append", default=[])
    run.add_argument("--limit", type=int, help="Maximum new cases in this batch; remainder can be resumed")
    resume = commands.add_parser("resume", help="Resume unattempted cases using the saved dataset snapshot")
    resume.add_argument("run_id")
    resume.add_argument("--limit", type=int)
    show = commands.add_parser("show", help="Read saved benchmark results")
    show.add_argument("run_id")
    show.add_argument("--details", action="store_true", help="Include case traces and individual field grades")
    compare = commands.add_parser("compare", help="Compare saved runs with matching datasets, selections, code and models")
    compare.add_argument("run_ids", nargs="+", help="Two or three saved runs, one per arm")
    simulate = commands.add_parser("simulate", help="Replay saved decisions offline against an independent synthetic ledger")
    simulate.add_argument("run_id")
    recovery = commands.add_parser("recovery", help="Run offline approved-workflow recovery episodes")
    recovery.add_argument("--case", action="append", default=[], dest="case_ids", help="Recovery episode ID; repeat to select")
    ablations = commands.add_parser("ablations", help="Compare local-only mechanism removal against the unchanged workflow")
    ablations.add_argument("--pair", action="append", default=[], dest="pair_ids", help="Ablation pair ID; repeat to select")


def execute(args):
    if args.benchmark_command == "validate":
        return validate_dataset(args.dataset, against=args.against, require_final=args.require_final)
    from . import benchmark_runner as runner
    if args.benchmark_command == "ablations":
        from .ablations import run_ablations
        return run_ablations(pair_ids=args.pair_ids)
    if args.benchmark_command == "recovery":
        from .recovery import run_suite
        return run_suite(case_ids=args.case_ids)
    if args.benchmark_command == "show":
        return runner.report(args.run_id, details=args.details)
    if args.benchmark_command == "compare":
        return runner.compare(args.run_ids)
    if args.benchmark_command == "simulate":
        from .benchmark_simulator import simulate_run
        return simulate_run(args.run_id)
    if args.limit is not None and args.limit < 1:
        raise DatasetError("invalid_limit", "Batch limit must be a positive integer.")
    if args.benchmark_command == "run":
        model = args.model
        if args.arm != "rules" and model is None:
            from .config import Settings
            model = Settings.from_environment().gemini_model
        run_id = runner.create_run(args.dataset, case_ids=args.case_ids, against=args.against, arm=args.arm, model=model)
    else:
        run_id = args.run_id
    return runner.resume(run_id, limit=args.limit)
