"""Versioned offline recovery experiments using the real approval/M1 workflow."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from threading import Barrier
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from .analysis import DryRunPolicy
from .benchmark import DatasetError, StrictRecord, read_json
from .config import PROJECT_ROOT
from .execution import ExecutionStore, ReviewedExecution
from .intent import Intent
from .recovery_provider import RecoveryProvider
from .stripe_gateway import SandboxError
from .workflow import REPLAY_WINDOW_SECONDS, SandboxWorkflow

SUITE = PROJECT_ROOT / "evals" / "recovery" / "episodes.json"
VERSION = "workflow-recovery-v1"


class Episode(StrictRecord):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    condition: Literal["lost_response", "known_id_read", "duplicate_execution", "refund_status", "expired_unknown", "state_change"]
    fault: Literal["after_refund", "before_refund", "refund_read", "payment_read", "repeat", "concurrent", "pending", "failed", "external_refund", "disputed"]
    amount: int = Field(gt=0, le=1000)
    expected_steps: list[str] = Field(min_length=1, max_length=2)
    expected_posts: int = Field(ge=0, le=2)
    expected_effects: int = Field(ge=0, le=1)
    expected_external_amount: int = Field(ge=0, le=1000)
    expected_local_status: Literal["verified", "failed", "unresolved", "approved"]


def load_suite():
    raw, data = read_json(SUITE)
    try:
        episodes = [Episode.model_validate(item) for item in data["episodes"]]
        if len(episodes) != 12 or len({item.id for item in episodes}) != 12:
            raise ValueError
        counts = Counter(item.condition for item in episodes)
        if len(counts) != 6 or set(counts.values()) != {2}:
            raise ValueError
    except (KeyError, TypeError, ValueError, ValidationError):
        raise DatasetError("invalid_recovery_suite", "Recovery suite must contain twelve valid unique episodes, two per condition.") from None
    return data, episodes, hashlib.sha256(raw).hexdigest()


def judge(episode, steps, final, local, post_invariants):
    effects = [row for row in final["refunds"] if row["source"] == "workflow" and row["status"] == "succeeded"]
    expected = [("pi_recovery", episode.amount, "usd")] * episode.expected_effects
    observed = [(row["payment_intent"], row["amount"], row["currency"]) for row in effects]
    false_success = 0
    for step in steps:
        if step["outcome"] != "verified":
            continue
        claim = step.get("refund") or {}
        matching = [row for row in step["ledger"]["refunds"] if row["id"] == claim.get("id")
                    and row["source"] == "workflow" and row["status"] == "succeeded"
                    and row["amount"] == episode.amount and row["payment_intent"] == "pi_recovery"
                    and row["currency"] == "usd" and claim.get("amount") == row["amount"]]
        false_success += len(matching) != 1
    posts = final["posts"]
    checks = {
        "expected_step_outcomes": [step["outcome"] for step in steps] == episode.expected_steps,
        "expected_post_count": len(posts) == episode.expected_posts,
        "expected_ledger_effects": sorted(observed) == sorted(expected),
        "expected_final_balance": final["payments"][0]["amount_refunded"] == episode.expected_external_amount + episode.amount * episode.expected_effects,
        "expected_journal_status": local["status"] == episode.expected_local_status,
        "immutable_approval": local["approval"]["amount_minor"] == episode.amount and local["approval"]["payment_id"] == "pi_recovery" and local["approval"]["currency"] == "usd",
        "journal_committed_before_posts": len(post_invariants) == len(posts) and all(post_invariants),
        "one_key_and_parameters": len({(row["operation_key"], row["params_json"]) for row in posts}) <= 1,
        "no_false_success": false_success == 0,
    }
    if episode.condition == "known_id_read":
        checks["known_id_survives_read_failure"] = any(
            row["id"] == steps[0]["local"]["stripe_id"] and row["source"] == "workflow"
            for row in steps[0]["ledger"]["refunds"])
    if episode.condition in ("lost_response", "expired_unknown"):
        checks["lost_response_keeps_unknown_id"] = steps[0]["local"]["stripe_id"] is None
    return {"passed": all(checks.values()), "checks": checks, "false_successes": false_success,
            "workflow_effects": len(effects), "duplicate_effects": max(0, len(effects) - 1),
            "workflow_refund_posts": len(posts)}


def run_episode(episode, directory, *, workflow_factory=None):
    store = ExecutionStore(Path(directory) / "application.sqlite3")
    provider = RecoveryProvider(Path(directory) / "provider.sqlite3")
    SandboxWorkflow(store, provider).create_payment("fixture", 1000)
    full = episode.amount == 1000
    amount = f"{episode.amount // 100}.{episode.amount % 100:02d}"
    message = "Refund the full remaining amount from pi_recovery" if full else f"Refund USD {amount} from pi_recovery"
    intent = Intent(intent="refund", payment_ids=["pi_recovery"], order_id=None,
                    refund_type="full" if full else "partial", amount=None if full else amount,
                    amount_evidence=None if full else f"USD {amount}", currency=None if full else "usd",
                    ambiguous=False, reason=None)
    request = store.create(message)
    analysis = store.start_analysis(request["id"], "synthetic-reference")
    analysis.update(status="completed", intent=intent.model_dump(),
                    decision=DryRunPolicy(store, provider.get_payment).assess(message, intent))
    store.finish_analysis(analysis)
    approval = store.approve(analysis["id"], "pi_recovery", episode.amount, "usd")
    invariants = []

    def before_post(params, key):
        with store.connect() as connection:
            row = connection.execute("SELECT params_json FROM stripe_operations WHERE stripe_key=?", (key,)).fetchone()
            committed_approval = connection.execute("SELECT 1 FROM refund_approvals WHERE operation_id=?", (approval["operation_id"],)).fetchone()
        invariants.append(row is not None and json.loads(row[0]) == params and committed_approval is not None)

    provider.before_post = before_post

    def execute(*, verify_only=False):
        # Reopen application state for every attempt, including after an injected failure.
        restarted_store = ExecutionStore(store.path)
        try:
            service = ReviewedExecution(restarted_store, provider)
            if workflow_factory is not None:
                service.workflow = workflow_factory(restarted_store, provider)
            result = service.execute(analysis["id"], verify_only=verify_only)
            step = {"outcome": result["status"], "refund": (result.get("result") or {}).get("refund")}
        except SandboxError as error:
            step = {"outcome": error.code}
        step.update(ledger=provider.snapshot(), local=restarted_store.execution(analysis["id"]))
        return step

    condition, fault = episode.condition, episode.fault
    if condition in ("lost_response", "known_id_read", "expired_unknown"):
        provider.fail_once(fault)
    elif condition == "refund_status":
        provider.next_refund_status = fault
    elif condition == "state_change":
        if fault == "external_refund":
            provider.external_refund(800)
        else:
            provider.dispute()

    if fault == "concurrent":
        barrier = Barrier(2)

        def simultaneous():
            barrier.wait(timeout=10)
            return execute()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(simultaneous) for _ in range(2)]
            steps = [future.result(timeout=20) for future in futures]
    else:
        steps = [execute()]
        if condition == "expired_unknown":
            with store.connect() as connection:
                connection.execute("UPDATE stripe_operations SET created_at=? WHERE operation_id=?",
                                   (time.time() - REPLAY_WINDOW_SECONDS - 1, approval["operation_id"]))
        if fault == "pending":
            provider.settle_pending()
        if condition != "state_change":
            steps.append(execute(verify_only=condition == "known_id_read" or fault == "pending"))
    final, local = provider.snapshot(), store.execution(analysis["id"])
    return {"id": episode.id, "condition": condition, "status": "completed", "specification": episode.model_dump(),
            "steps": steps, "ledger": final, "local": local, "post_journal_checks": invariants,
            **judge(episode, steps, final, local, invariants)}


def run_suite(*, case_ids=(), output_directory=None):
    data, episodes, dataset_hash = load_suite()
    if len(set(case_ids)) != len(case_ids) or set(case_ids) - {item.id for item in episodes}:
        raise DatasetError("invalid_recovery_selection", "Select unique IDs from the recovery suite.")
    selected = [item for item in episodes if not case_ids or item.id in case_ids]
    digest = hashlib.sha256()
    for name in ("recovery.py", "recovery_provider.py", "workflow.py", "execution.py", "analysis.py", "operations.py", "storage.py", "intent.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    report = {"run_id": uuid4().hex, "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "suite_version": data["version"], "suite_review_status": data["review_status"],
              "suite_sha256": dataset_hash, "code_sha256": digest.hexdigest(), "python_version": platform.python_version(),
              "scope": "offline_approved_workflow_recovery", "status": "incomplete", "scheduled": len(selected),
              "model_calls": 0, "stripe_calls": 0, "episodes": []}
    output_directory = Path(output_directory or PROJECT_ROOT / "instance" / "benchmarks" / "recovery")
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"{report['run_id']}.json"

    def save():
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    for episode in selected:
        try:
            with TemporaryDirectory(prefix="payresolve-recovery-") as directory:
                result = run_episode(episode, directory)
        except Exception:
            result = {"id": episode.id, "condition": episode.condition, "status": "error", "passed": False,
                      "error": {"code": "recovery_episode_failed"}}
        report["episodes"].append(result)
        save()
    rows = report["episodes"]
    report.update(status="completed" if all(row["status"] == "completed" for row in rows) else "incomplete",
                  completed=sum(row["status"] == "completed" for row in rows), passed=sum(row["passed"] for row in rows),
                  errors=sum(row["status"] == "error" for row in rows),
                  false_successes=sum(row.get("false_successes", 0) for row in rows),
                  duplicate_effects=sum(row.get("duplicate_effects", 0) for row in rows))
    report["by_condition"] = {condition: {"episodes": sum(row["condition"] == condition for row in rows),
                                        "passed": sum(row["condition"] == condition and row["passed"] for row in rows)}
                              for condition in sorted({row["condition"] for row in rows})}
    save()
    return {key: value for key, value in report.items() if key != "episodes"} | {"report_path": str(path.resolve())}
