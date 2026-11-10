"""Local-only mechanism ablations; these variants cannot use a Stripe gateway."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from .benchmark import DatasetError, StrictRecord, read_json
from .config import PROJECT_ROOT
from .recovery import load_suite, run_episode
from .recovery_provider import RecoveryProvider
from .workflow import REPLAY_WINDOW_SECONDS, SandboxWorkflow

SPECIFICATION = PROJECT_ROOT / "evals" / "recovery" / "ablations.json"
VERSION = "matched-recovery-ablations-v1"


class LocalOnlyWorkflow(SandboxWorkflow):
    def __init__(self, store, gateway):
        if type(gateway) is not RecoveryProvider:
            raise ValueError("Ablations require the exact local RecoveryProvider.")
        super().__init__(store, gateway)


class FreshRetryKeyWorkflow(LocalOnlyWorkflow):
    """Only unknown-ID retries inside the allowed window receive a new key."""

    def _execute(self, operation_id, *, read_only=False):
        if not read_only:
            with self.store.locked() as connection:
                op = self.store.operation(connection, operation_id)
                if (op and op["kind"] == "refund" and not op["stripe_id"] and op["last_error"]
                        and op["status"] == "unresolved" and time.time() - op["created_at"] < REPLAY_WINDOW_SECONDS):
                    key = str(uuid4())
                    params = json.loads(op["params_json"])
                    # Keep metadata aligned with the replacement identity, so verification
                    # still measures the resulting refund rather than an artificial mismatch.
                    params["metadata"]["payresolve_operation"] = key
                    connection.execute("UPDATE stripe_operations SET stripe_key=?,params_json=? WHERE operation_id=?",
                                       (key, json.dumps(params, sort_keys=True), operation_id))
        return super()._execute(operation_id, read_only=read_only)


class SkipFinalVerificationWorkflow(LocalOnlyWorkflow):
    """Model the flawed assumption that obtaining a refund ID means success."""

    def _verify(self, connection, op, params):
        if op["kind"] != "refund":
            return super()._verify(connection, op, params)
        before = json.loads(op["before_json"])
        return {"operation_id": op["operation_id"], "status": "verified", "verification_skipped": True,
                "refund": {"id": op["stripe_id"], "payment_intent": params["payment_intent"],
                           "amount": params["amount"], "currency": before["currency"],
                           "charge": before["charge_id"], "operation_key": op["stripe_key"],
                           "status": "assumed_succeeded"}}


VARIANTS = {"fresh_retry_key": FreshRetryKeyWorkflow, "skip_final_verification": SkipFinalVerificationWorkflow}


class Pair(StrictRecord):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    episode_id: str
    variant: Literal["fresh_retry_key", "skip_final_verification"]
    hypothesis: str = Field(min_length=10, max_length=1000)


def run_ablations(*, pair_ids=(), output_directory=None):
    suite, episodes, suite_hash = load_suite()
    raw, data = read_json(SPECIFICATION)
    try:
        pairs = [Pair.model_validate(item) for item in data["pairs"]]
        if (len(pairs) != 4 or len({item.id for item in pairs}) != 4
                or any(item.episode_id not in {episode.id for episode in episodes} for item in pairs)):
            raise ValueError
    except (KeyError, TypeError, ValueError, ValidationError):
        raise DatasetError("invalid_ablation_specification", "Expected four valid unique pairs referencing recovery episodes.") from None
    if len(set(pair_ids)) != len(pair_ids) or set(pair_ids) - {item.id for item in pairs}:
        raise DatasetError("invalid_pair_selection", "Select unique IDs from the ablation specification.")
    pairs = [item for item in pairs if not pair_ids or item.id in pair_ids]
    digest = hashlib.sha256()
    for name in ("ablations.py", "recovery.py", "recovery_provider.py", "workflow.py", "execution.py",
                 "analysis.py", "operations.py", "storage.py", "intent.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    report = {"run_id": uuid4().hex, "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "specification_version": data["version"], "review_status": data["review_status"],
              "specification_sha256": hashlib.sha256(raw).hexdigest(), "suite_sha256": suite_hash,
              "code_sha256": digest.hexdigest(), "python_version": platform.python_version(),
              "scope": "matched_local_workflow_ablations", "status": "incomplete", "scheduled_pairs": len(pairs),
              "model_calls": 0, "stripe_calls": 0, "pairs": []}
    output_directory = Path(output_directory or PROJECT_ROOT / "instance" / "benchmarks" / "ablations")
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"{report['run_id']}.json"

    def save():
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    for pair in pairs:
        episode = next(item for item in episodes if item.id == pair.episode_id)
        row = {"id": pair.id, "variant": pair.variant, "hypothesis": pair.hypothesis, "episode_id": pair.episode_id}
        for arm, factory in (("baseline", None), ("ablated", VARIANTS[pair.variant])):
            try:
                with TemporaryDirectory(prefix="payresolve-ablation-") as directory:
                    row[arm] = run_episode(episode, directory, workflow_factory=factory)
            except Exception:
                row[arm] = {"status": "error", "passed": False, "error": {"code": "ablation_episode_failed"}}
            # Keep each completed arm if the process is interrupted before its pair finishes.
            if row not in report["pairs"]:
                report["pairs"].append(row)
            save()
    rows = report["pairs"]
    complete = sum(all(row[arm]["status"] == "completed" for arm in ("baseline", "ablated")) for row in rows)
    report.update(status="completed" if complete == len(pairs) else "incomplete", completed_pairs=complete,
                  baseline_passed=sum(row["baseline"]["passed"] for row in rows),
                  ablated_passed=sum(row["ablated"]["passed"] for row in rows))
    report["by_variant"] = {}
    for variant in sorted({row["variant"] for row in rows}):
        selected = [row for row in rows if row["variant"] == variant]
        report["by_variant"][variant] = {"pairs": len(selected)}
        for arm in ("baseline", "ablated"):
            report["by_variant"][variant][arm] = {
                "protocol_passed": sum(row[arm]["passed"] for row in selected),
                "duplicate_effects": sum(row[arm].get("duplicate_effects", 0) for row in selected),
                "false_success_reports": sum(row[arm].get("false_successes", 0) for row in selected),
                "episodes_with_false_success": sum(row[arm].get("false_successes", 0) > 0 for row in selected)}
    report["note"] = "Ablated protocol failures are experimental results, not harness errors. Four targeted draft pairs do not establish general failure rates."
    save()
    return {key: value for key, value in report.items() if key != "pairs"} | {"report_path": str(path.resolve())}
