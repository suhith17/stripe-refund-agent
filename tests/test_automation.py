from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from payresolve import create_app
from payresolve.agent import AgentRunner
from payresolve.automation import CaseAutomation
from payresolve.cases import CaseStore, CaseService
from payresolve.catalog import seed_catalog
from payresolve.config import Settings
from payresolve.gemini_gateway import ExtractionError
from payresolve.workflow import REPLAY_WINDOW_SECONDS
from tests.test_agent import ScriptedPlanner
from tests.test_workflow import FakeStripe


class Unavailable:
    model='offline-test'
    def step(self,*args,**kwargs):
        raise ExtractionError('gemini_unavailable','Offline test failure')


class AutomationTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=Settings(Path(temp.name)/'jobs.db')
        self.store=CaseStore(self.settings.database_path)
        self.gateway=FakeStripe()
        seed_catalog(self.store.catalog,self.gateway,'auto')
        self.customer='auto:a'
        self.case=self.store.create_case(self.customer,'Refund my Friday headphones')
        self.clock=[time.time()]
        self.planner=Mock(return_value=ScriptedPlanner([{'action':'ask','reason':'purchase'}]))
        self.worker=self.make_worker()

    def make_worker(self):
        return CaseAutomation(self.settings,gateway_factory=lambda:self.gateway,planner_factory=self.planner,clock=lambda:self.clock[0])

    def advance(self,seconds=200):
        self.clock[0]+=seconds
        return self.worker.tick()

    def proposal(self):
        return CaseService(self.store,self.gateway).propose(self.customer,self.case['id'],'ORD-102',1000,'usd',self.case['version'])

    def test_no_confirmation_means_no_automatic_refund(self):
        case=self.proposal()
        self.worker.tick()
        self.advance()
        self.assertEqual(self.worker.jobs(self.customer,case['id']),[])
        self.assertEqual(self.gateway.refund_posts,[])

    def test_claim_survives_restart_and_completes_without_customer_click(self):
        case=self.proposal()
        self.store.claim(self.customer,case['id'],case['revision'],case['version'])
        self.worker=self.make_worker()
        self.worker.tick()
        self.advance()
        result=self.store.snapshot(self.customer,case['id'])
        self.assertEqual(result['stage'],'verified')
        self.assertIn('verified by Stripe',result['messages'][-1]['content'])
        self.advance()
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_lost_post_response_recovers_same_effect_automatically(self):
        case=self.proposal()
        self.gateway.lose_response=True
        CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        self.worker.tick()
        self.advance()
        self.assertEqual(self.store.snapshot(self.customer,case['id'])['stage'],'verified')
        self.assertEqual(len(self.gateway.refunds),1)
        self.assertEqual(self.gateway.refund_posts[0][1],self.gateway.refund_posts[1][1])

    def test_pending_is_polled_until_verified_without_another_post(self):
        case=self.proposal()
        self.gateway.refund_status='pending'
        result=CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        self.worker.tick()
        refund=self.gateway.refunds[result['refund_id']]
        refund['status']='succeeded'
        payment=self.gateway.payments[refund['payment_intent']]
        payment['amount_refunded']+=refund['amount']
        payment['refundable_amount']-=refund['amount']
        self.advance()
        self.assertEqual(self.store.snapshot(self.customer,case['id'])['stage'],'verified')
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_pending_budget_survives_restart_and_expired_unknown_is_not_replayed(self):
        case=self.proposal()
        self.gateway.refund_status='pending'
        CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        self.worker.tick()
        for _ in range(3):
            self.worker=self.make_worker()
            self.advance()
        job=self.worker.jobs(self.customer,case['id'])[0]
        self.assertEqual((job['status'],job['attempts']),('exhausted',3))
        self.advance()
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_expired_unknown_requires_reconciliation(self):
        case=self.proposal()
        self.gateway.lose_response=True
        result=CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        with self.store.connect() as c:
            c.execute('UPDATE stripe_operations SET created_at=? WHERE operation_id=?',(time.time()-REPLAY_WINDOW_SECONDS-1,result['operation_id']))
        self.worker.tick()
        self.advance()
        result=self.store.snapshot(self.customer,case['id'])
        self.assertEqual(result['last_error'],'reconciliation_required')
        self.assertEqual(self.worker.jobs(self.customer,case['id'])[0]['status'],'stopped')
        self.advance()
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_model_failure_retries_once_and_does_not_reset_budget(self):
        AgentRunner(self.store,self.gateway,Unavailable()).run(self.customer,self.case['id'])
        self.planner.return_value=Unavailable()
        self.worker.tick()
        self.advance()
        self.worker=self.make_worker()
        self.advance()
        self.assertEqual(self.planner.call_count,1)
        self.assertEqual(self.worker.jobs(self.customer,self.case['id'])[0]['status'],'exhausted')
        self.assertEqual(len(self.store.snapshot(self.customer,self.case['id'])['agent_turns']),2)

    def test_new_message_supersedes_queued_retry(self):
        case=AgentRunner(self.store,self.gateway,Unavailable()).run(self.customer,self.case['id'])
        self.worker.tick()
        self.store.message(self.customer,case['id'],'Actually do not refund.',case['version'])
        self.advance()
        self.planner.assert_not_called()
        self.assertEqual(self.worker.jobs(self.customer,case['id'])[0]['status'],'superseded')

    def test_abandoned_agent_turn_is_resumed_after_lease_expiry(self):
        case=AgentRunner(self.store,self.gateway,Unavailable()).run(self.customer,self.case['id'])
        with self.store.connect() as c:
            c.execute("UPDATE agent_turns SET status='running',case_version=?,expires_at=0 WHERE case_id=?",(case['version'],case['id']))
        self.worker.tick()
        self.advance()
        self.assertEqual(self.store.snapshot(self.customer,case['id'])['stage'],'awaiting_clarification')
        self.planner.assert_called_once()

    def test_local_poll_is_scoped_read_only_and_normal_screen_has_no_retry_buttons(self):
        app=create_app(self.settings)
        client=app.test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id']=self.customer
        path='/demo/cases/'+self.case['id']
        with patch('payresolve.dashboard.StripeSandbox',side_effect=AssertionError('GET must not call Stripe')):
            response=client.get(path+'/state')
            page=client.get(path)
        self.assertEqual(response.status_code,200)
        self.assertNotIn(b'Retry agent',page.data)
        with client.session_transaction() as session:
            session['demo_customer_id']='auto:b'
        self.assertEqual(client.get(path+'/state').status_code,404)


if __name__=='__main__':
    unittest.main()
