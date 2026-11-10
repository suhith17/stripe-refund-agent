"""Real backup model, injected primary outage, isolated simulated payments.

Run explicitly with: python -m tools.check_chat_backup
This uses Gemini quota. It never constructs a Stripe client.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from payresolve.agent import AgentRunner, GeminiPlanner
from payresolve.cases import CaseStore, CaseService
from payresolve.catalog import seed_catalog
from payresolve.config import PROJECT_ROOT, Settings
from payresolve.conversation_provider import ConversationProvider
from payresolve.gemini_gateway import ExtractionError
from payresolve.model_runtime import configured_planner


class InjectedOutage:
    def __init__(self, model):
        self.model = model

    def step(self, context, **kwargs):
        raise ExtractionError('gemini_service_unavailable', 'Injected primary outage; no primary HTTP request.', attempts=0)


def main():
    settings = Settings.from_environment()
    if not settings.agent_fallback_model:
        print(json.dumps({'status': 'backup_not_configured'}))
        return 1
    directory = PROJECT_ROOT / 'instance' / 'backup-checks' / uuid4().hex
    directory.mkdir(parents=True)
    planner = configured_planner(settings, GeminiPlanner)
    planner.primary = InjectedOutage(settings.gemini_model)
    store = CaseStore(directory / 'application.db')
    provider = ConversationProvider(directory / 'provider.db')
    seed_catalog(store.catalog, provider, 'backup-check')
    customer = 'backup-check:a'
    case = store.create_case(customer, 'Refund USD 5 from ORD-102.')
    case = AgentRunner(store, provider, planner).run(customer, case['id'])
    before = provider.snapshot()
    valid = (case['stage'] == 'awaiting_confirmation' and case['selected_order'] == 'ORD-102'
             and case['amount_minor'] == 500 and not before['posts'])
    repeated = False
    if valid:
        card = dict(case)
        case = CaseService(store, provider).confirm(customer, case['id'], card['revision'], card['version'])
        again = CaseService(CaseStore(directory / 'application.db'), provider).confirm(
            customer, case['id'], card['revision'], card['version'])
        repeated = case['refund_id'] == again['refund_id'] and case['stage'] == again['stage'] == 'verified'
    ledger = provider.snapshot()
    attempts = [e['details'] for e in case['events'] if e['kind'] == 'agent_model_attempt']
    switches = [e for e in case['events'] if e['kind'] == 'agent_model_fallback']
    report = {'run_id': directory.name, 'created_at': datetime.now(timezone.utc).isoformat(),
              'evidence_type': 'live_backup_model_injected_primary_outage_simulated_payments',
              'stripe_calls': 0, 'backup_model': settings.agent_fallback_model,
              'status': 'passed' if valid and repeated and len(ledger['refunds']) == 1 and len(switches) == 1 else 'failed',
              'turn_status': case['agent_turns'][-1]['status'], 'final_stage': case['stage'],
              'correct_proposal_without_execution': valid, 'repeat_returned_existing_refund': repeated,
              'refund_effects': len(ledger['refunds']), 'switches': len(switches), 'attempts': attempts}
    (directory / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
