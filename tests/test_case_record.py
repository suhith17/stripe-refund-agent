from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve import create_app
from payresolve.cases import CaseStore
from payresolve.catalog import seed_catalog
from payresolve.config import Settings
from tests.test_workflow import FakeStripe


class CaseRecordTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(Path(temp.name) / 'cases.db', gemini_api_key='configured-secret-marker')
        self.store = CaseStore(self.settings.database_path)
        seed_catalog(self.store.catalog, FakeStripe(), 'export')
        self.case = self.store.create_case('export:a', 'Refund my headphones')
        self.client = create_app(self.settings).test_client()
        self.client.get('/demo')
        with self.client.session_transaction() as session:
            session['demo_customer_id'] = 'export:a'
        self.url = f"/demo/cases/{self.case['id']}/record"

    def test_download_preserves_evidence_and_redacts_old_credentials_without_provider_calls(self):
        with self.store.locked() as c:
            self.store.event(c, self.case['id'], 'old_observation', {
                'nested': ['configured-secret-marker', 'sk_test_oldcredential'], 'safe': 'retained'})
        with patch('payresolve.dashboard.StripeSandbox', side_effect=AssertionError('No provider calls')):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        result = response.get_json()
        self.assertEqual(result['schema_version'], 'case-record-v1')
        self.assertEqual(result['case']['id'], self.case['id'])
        self.assertEqual(result['case']['messages'][0]['content'], 'Refund my headphones')
        self.assertEqual(result['case']['events'][-1]['details'], {
            'nested': ['[redacted]', '[redacted]'], 'safe': 'retained'})
        self.assertNotIn(b'configured-secret-marker', response.data)

    def test_other_customer_and_missing_identity_cannot_download(self):
        for customer in ('export:b', None):
            with self.client.session_transaction() as session:
                session['demo_customer_id'] = customer
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b'Refund my headphones', response.data)
