"""Customer-facing summaries of recorded case state, never model success claims."""


def present_case(case):
    stage = case['stage']
    known_order = case.get('remembered_details', {}).get('order')
    current = (5 if stage == 'verified' else 4 if case['refund_id'] else 3 if case['operation_id']
               else 2 if stage == 'awaiting_confirmation' else 1 if known_order else 0)
    titles = {
        'collecting_details': ('Let’s find the right refund', 'Tell the agent about your purchase. It will work through the next steps.'),
        'awaiting_clarification': ('One more detail', case['question'] or 'Reply in the conversation to continue.'),
        'awaiting_confirmation': ('Your refund is ready to review', 'Check the purchase and amount. Confirm only when they are right.'),
        'processing': ('Working on your confirmed refund', 'The existing operation is saved. Its outcome is not verified yet.'),
        'pending': ('Waiting for the refund to finish', 'Stripe has not confirmed completion. The agent will check within its recovery limits.'),
        'needs_reconciliation': ('Checking an uncertain result', 'Your operation is saved. A verified result is not available yet.'),
        'verified': ('Your refund is verified', 'Retrieved Stripe state confirmed this refund. This workspace uses sandbox funds.'),
        'cancelled': ('This case is closed', 'No refund was authorized through this case.'),
        'failed': ('This refund needs attention', 'Review the recorded outcome below before continuing.'),
    }
    title, description = titles.get(stage, ('Your refund case', 'Review the latest recorded state below.'))
    steps = [{'label': label, 'state': 'done' if index < current else 'current' if index == current and stage not in ('cancelled', 'failed') else 'upcoming'}
             for index, label in enumerate(('Find purchase', 'Prepare refund', 'Your confirmation', 'Process refund', 'Verify result'))]
    activity = []
    live = None
    for event in case['events']:
        kind, detail = event['kind'], event['details']
        label, summary, tone = None, '', 'done'
        if kind == 'agent_model_started':
            live = 'Reading your message and deciding the next step…'
        elif kind == 'agent_model_fallback':
            label, summary = 'Switched to the backup model', 'The primary model was temporarily unavailable. Continuing with the same case and refund checks.'
        elif kind == 'agent_waiting_for_customer':
            label, summary = 'Waiting for your instructions', 'Refund preparation is paused until you clarify your request.'
        elif kind == 'agent_loop_stopped':
            label, summary = 'Asked for clarification', 'Stopped repeating the same action and requested the missing detail.'
        elif kind == 'agent_tool_started':
            live = {'search_orders': 'Searching your purchases…', 'inspect_payment': 'Reading the selected payment…',
                    'propose_refund': 'Checking the refund proposal…', 'get_refund_status': 'Retrieving the existing refund status…'}.get(detail.get('tool'), 'Working on your case…')
        elif kind == 'agent_turn_finished':
            live = None
            if detail.get('status') not in ('completed', 'superseded'):
                label, summary, tone = 'Agent turn interrupted', 'The case is saved. Check the current recovery status.', 'attention'
        elif kind == 'agent_tool_result':
            if detail.get('error'):
                label, summary, tone = 'A check needs attention', 'The agent could not use this result to proceed.', 'attention'
            elif detail.get('tool') == 'search_orders':
                label, summary = 'Searched your purchases', f"{len(detail.get('orders', []))} matching purchases found."
            elif detail.get('tool') == 'inspect_payment':
                label, summary = 'Read payment details', 'Retrieved the purchase’s current payment state.'
        elif kind == 'case_memory_saved':
            label, summary = 'Remembered your details', 'Saved: ' + ', '.join(detail.get('details', {}).keys()) + '.'
        elif kind == 'case_memory_invalidated':
            label, summary = 'Updated after your correction', 'Cleared earlier ' + ', '.join(detail.get('fields', [])) + ' details.'
        elif kind == 'case_memory_rejected':
            label, summary, tone = 'Discarded an unsupported detail', 'A detail did not match your messages and was not saved. The agent can still ask for clarification.', 'attention'
        elif kind == 'clarification_requested':
            label, summary = 'Asked for clarification', detail.get('question', '')
        elif kind == 'proposal_validated':
            label, summary = 'Prepared a refund proposal', f"{detail['order_id']} · USD {detail['amount_minor'] / 100:.2f}. Waiting for your confirmation."
        elif kind == 'confirmation_received':
            label, summary = 'Received your confirmation', 'Your exact proposal is linked to one refund operation.'
        elif kind == 'operation_observed':
            verified = detail.get('stage') == 'verified'
            label = 'Verified the refund' if verified else 'Checked the refund outcome'
            summary = 'Retrieved state confirms success.' if verified else 'Observed state: ' + detail.get('stage', 'unknown').replace('_', ' ') + '.'
            tone = 'done' if verified else 'attention'
        elif kind == 'automatic_recovery_started':
            label, summary = 'Started automatic recovery', 'Continuing saved work within the retry limits.'
        elif kind == 'case_cancelled':
            label, summary = 'Closed the case', 'No refund authorized through this case.'
        if label:
            activity.append({'title': label, 'summary': summary, 'tone': tone,
                             'time': event['created_at'][11:19], 'details': detail})
    return {'title': title, 'description': description, 'steps': steps,
            'activity': list(reversed(activity[-10:])), 'live_activity': live}
