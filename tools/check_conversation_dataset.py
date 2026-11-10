"""Offline fixture/monetary-label preflight, not a model or language evaluation."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from payresolve.analysis import DryRunPolicy
from payresolve.cases import CaseStore
from payresolve.catalog import seed_catalog
from payresolve.conversation_eval import Dataset
from payresolve.conversation_provider import ConversationProvider
from payresolve.stripe_gateway import SandboxError


def inspect_dataset(path):
    raw = path.read_bytes()
    dataset = Dataset.model_validate_json(raw)
    parent = ROOT / 'instance/dataset-preflight'
    parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with TemporaryDirectory(prefix='fixtures-', dir=parent) as temporary:
        for scenario in dataset.scenarios:
            directory = Path(temporary) / scenario.id
            directory.mkdir()
            store = CaseStore(directory / 'app.db')
            provider = ConversationProvider(directory / 'provider.db')
            if scenario.fixture:
                provider.seed_fixture(store.catalog, scenario.fixture)
            else:
                seed_catalog(store.catalog, provider, 'conversation')
            customer = f'conversation:{scenario.customer}'
            policy = DryRunPolicy(store, provider.get_payment)
            checks, last_card = [], None
            for index, step in enumerate(scenario.steps):
                if step.kind == 'message' and step.expected_stage == 'awaiting_confirmation':
                    last_card = step
                    inspected = store.catalog.inspect_payment(customer, step.order_id, provider)
                    decision = policy.assess_payment(inspected['payment']['id'], step.amount_minor)
                    checks.append({'step': index, 'check': 'declared_card_payment_eligibility',
                                   'code': decision['code'], 'valid': decision['action'] == 'propose_refund'})
                elif step.control and step.control.action in ('external_refund', 'dispute'):
                    control = step.control
                    order = store.catalog.order(customer, control.order_id)
                    if control.action == 'external_refund':
                        provider.external_refund_for(order['payment_id'], control.amount_minor, f'preflight-{index}')
                    else:
                        provider.dispute_payment(order['payment_id'])
                elif step.kind == 'confirm' and scenario.expected_outcome == 'blocked_execution':
                    order = store.catalog.order(customer, last_card.order_id)
                    decision = policy.assess_payment(order['payment_id'], last_card.amount_minor)
                    checks.append({'step': index, 'check': 'declared_execution_block', 'code': decision['code'],
                                   'valid': decision['action'] != 'propose_refund' and decision['code'] == step.expected_error})
            ledger = provider.snapshot()
            # Preflight must never submit an agent refund, even to the simulator.
            assert not ledger['posts'] and all(r['source'] == 'external' for r in ledger['refunds'])
            rows.append({'scenario_id': scenario.id, 'checks': checks,
                         'fixture_labels_valid': all(c['valid'] for c in checks)})
    return {'kind': 'offline_fixture_label_preflight', 'dataset_sha256': sha256(raw).hexdigest(),
            'scenarios': len(rows), 'declared_card_checks': sum(c['check'] == 'declared_card_payment_eligibility' for r in rows for c in r['checks']),
            'declared_block_checks': sum(c['check'] == 'declared_execution_block' for r in rows for c in r['checks']),
            'invalid_checks': sum(not c['valid'] for r in rows for c in r['checks']),
            'model_calls': 0, 'stripe_calls': 0, 'agent_refund_posts': 0,
            'scope': 'Checks declared cards against scoped fixture/payment policy and declared external balance changes. '
                     'Does not infer intent, check grounding/retrieval choices, score clarification language, '
                     'or execute full conversations. Rows with no card/block checks validate fixture construction only.',
            'cases': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--report', type=Path, help='Optional offline report, normally under instance/')
    args = parser.parse_args()
    try:
        report = inspect_dataset(args.dataset)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k != 'cases'}, indent=2))
        return 1 if report['invalid_checks'] else 0
    except (ValueError, OSError, SandboxError, sqlite3.Error) as error:
        print(json.dumps({'error': 'invalid_dataset_or_fixture_preflight',
                          'category': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
