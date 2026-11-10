import unittest

from payresolve.agent_contract import AgentAction, ground_order, references
from payresolve.stripe_gateway import SandboxError
from tests import test_case_memory


ORDERS = [dict(order_id='ORD-101', payment_id='pi_a', purchased_on='2026-10-26', description='Wireless headphones'),
          dict(order_id='ORD-102', payment_id='pi_b', purchased_on='2026-10-30', description='Wireless headphones')]


class OrderDateTests(unittest.TestCase):
    def test_literal_named_date_variants_resolve_without_inventing_year(self):
        for text in ('October 30', 'Oct. 30th', '30 October', '30th of October, 2026', 'Friday October 30', '2026-10-30'):
            with self.subTest(text=text):
                self.assertEqual(references(text, ORDERS), {'ORD-102'})
        repeated = ORDERS + [{**ORDERS[1], 'order_id': 'ORD-103', 'purchased_on': '2025-10-30'}]
        self.assertEqual(references('October 30', repeated), {'ORD-102', 'ORD-103'})
        self.assertEqual(references('October 30, 2026', repeated), {'ORD-102'})

    def test_invalid_wrong_year_and_conflicting_weekday_never_fall_back_to_description(self):
        for text in ('headphones October 32', 'headphones February 30', 'headphones October 30, 2025', 'Monday October 30'):
            with self.subTest(text=text):
                self.assertEqual(references(text, ORDERS), set())


class DateMemoryTests(unittest.TestCase):
    setUp = test_case_memory.CaseMemoryTests.setUp
    run_action = test_case_memory.CaseMemoryTests.run_action
    remember_request = test_case_memory.CaseMemoryTests.remember_request
    reply = test_case_memory.CaseMemoryTests.reply

    def test_date_correction_clears_order_and_rejects_old_source_even_without_matching_candidate(self):
        case = self.remember_request()
        mid = case['messages'][0]['id']
        case = self.reply(case, 'Actually the October 29 purchase.')
        self.assertNotIn('order', case['remembered_details'])
        with self.assertRaises(SandboxError) as caught:
            ground_order(AgentAction(action='inspect_payment', order_id='ORD-102',
                         order_message_id=mid, order_evidence='ORD-102'), case, self.store.catalog, self.customer)
        self.assertEqual(caught.exception.code, 'conflicting_order_evidence')
