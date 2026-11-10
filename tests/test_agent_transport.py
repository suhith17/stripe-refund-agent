"""Exercise the installed SDK transport without network access."""
import unittest
from unittest.mock import patch

import httpx
from pathlib import Path
from tempfile import TemporaryDirectory
from google import genai

from payresolve.agent import GeminiPlanner
from payresolve.gemini_gateway import ExtractionError
from payresolve.model_runtime import RequestPacer


class AgentTransportTests(unittest.TestCase):
    def test_rate_limit_header_persists_cooldown_without_another_http_send(self):
        with TemporaryDirectory() as directory:
            pacer = RequestPacer(Path(directory) / 'pacing.db')
            seen = []
            def respond(request):
                seen.append(request)
                return httpx.Response(429, headers={'Retry-After': '90'}, json={'error': {'code': 429}})
            original = genai.Client
            def client(**options):
                options['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
                return original(**options)
            with patch('payresolve.gemini_gateway.genai.Client', side_effect=client):
                planner = GeminiPlanner('fake-key', 'gemini-test', pacer=pacer)
                for expected_attempts in (1, 0):
                    with self.assertRaises(ExtractionError) as caught:
                        planner.step({'messages': []}, remaining_seconds=20)
                    self.assertEqual(caught.exception.code, 'gemini_rate_limited')
                    self.assertEqual(caught.exception.attempts, expected_attempts)
            self.assertEqual(len(seen), 1)

    def test_service_error_uses_one_http_attempt(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(503, json={'error': {'code': 503, 'message': 'offline service failure'}})
        original = genai.Client
        def client(**options):
            options['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            return original(**options)
        with patch('payresolve.gemini_gateway.genai.Client', side_effect=client), patch('time.sleep'):
            with self.assertRaises(ExtractionError) as caught:
                GeminiPlanner('fake-key', 'gemini-3.5-flash-lite').step({'messages': []}, remaining_seconds=20)
        self.assertEqual(len(seen), 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.code, 'gemini_service_unavailable')

    def test_network_failure_cannot_send_a_second_request(self):
        seen = []
        def respond(request):
            seen.append(request)
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        original = genai.Client
        def client(**options):
            options['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            return original(**options)
        with patch('payresolve.gemini_gateway.genai.Client', side_effect=client), patch('time.sleep'):
            with self.assertRaises(ExtractionError) as caught:
                GeminiPlanner('fake-key', 'gemini-3.5-flash-lite').step({'messages': []}, remaining_seconds=20)
        self.assertEqual(len(seen), 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.code, 'gemini_unavailable')
