"""One bounded, stateless Gemini extraction; no Stripe tools or credentials."""

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google import genai
from pydantic import ValidationError

from .intent import Intent, PROMPT_VERSION, SYSTEM_INSTRUCTION


class ExtractionError(Exception):
    def __init__(self, code, message, *, attempts=0, latency_ms=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.attempts = attempts
        self.latency_ms = latency_ms


class _StopHttpRetry(Exception):
    """Transport boundary signal containing no upstream body or credentials."""
    def __init__(self, status_code=None):
        self.status_code = status_code


def validate_message(message, secrets=()):
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000 or "\x00" in message:
        raise ExtractionError("invalid_message", "Provide 1 to 4000 characters without null bytes.")
    if any(secret and secret in message for secret in secrets) or re.search(
        r"(?:[spr]k_(?:test|live)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{20,})", message,
    ):
        raise ExtractionError("credential_in_message", "Remove credentials from the request text.")
    return message.strip()


class GeminiExtractor:
    request_timeout_ms = 30000
    request_retries = 2
    response_schema = Intent
    system_instruction = SYSTEM_INSTRUCTION
    prompt_version = PROMPT_VERSION
    result_key = "intent"

    def __init__(self, api_key, model, *, forbidden_values=(), pacer=None):
        self._api_key = api_key
        self.model = model
        self._secrets = (api_key, *forbidden_values)
        self.pacer = pacer
        self.request_deadline = None
        if not re.fullmatch(r"gemini-[A-Za-z0-9.-]+", model):
            raise ExtractionError("invalid_gemini_model", "Set GEMINI_MODEL to a Gemini model identifier.")

    def validate(self, message):
        return validate_message(message, self._secrets)

    def extract(self, message):
        message = self.validate(message)
        return self._extract_validated(message)

    def _extract_validated(self, message):
        if not self._api_key:
            raise ExtractionError("gemini_not_configured", "Set GEMINI_API_KEY in your local .env file.")
        started = time.perf_counter()
        try:
            response, attempt = self._request(message)
        except ExtractionError as error:
            error.latency_ms = round((time.perf_counter() - started) * 1000)
            raise
        elapsed = round((time.perf_counter() - started) * 1000)
        if getattr(response, "status", None) != "completed":
            raise ExtractionError("gemini_incomplete", "Gemini did not return a completed extraction.",
                                  attempts=attempt, latency_ms=elapsed)
        try:
            intent = self.response_schema.model_validate_json(response.output_text)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise ExtractionError("invalid_model_output", "Gemini returned an invalid extraction; no action was proposed.",
                                  attempts=attempt, latency_ms=elapsed) from None
        # Do not persist a model response that somehow repeats configured secrets.
        if any(secret and secret in intent.model_dump_json() for secret in self._secrets):
            raise ExtractionError("invalid_model_output", "Gemini returned an invalid extraction; no action was proposed.",
                                  attempts=attempt, latency_ms=elapsed)
        usage = getattr(response, "usage", None)
        return {
            self.result_key: intent.model_dump(), "model": self.model, "prompt_version": self.prompt_version,
            "attempts": attempt, "latency_ms": elapsed,
            "pacing_wait_ms": getattr(self, 'pacing_wait_ms', 0),
            "usage": {name: getattr(usage, name, None) for name in
                      ("total_input_tokens", "total_output_tokens", "total_thought_tokens", "total_tokens")},
        }

    def _request(self, message):
        attempts = 0
        self.pacing_wait_ms = 0

        def record_request(request):
            nonlocal attempts
            if attempts >= self.request_retries + 1:
                raise _StopHttpRetry()
            if self.pacer is not None:
                deadline = self.request_deadline or (time.monotonic() + self.request_timeout_ms / 1000)
                self.pacing_wait_ms += self.pacer.wait(self.model, deadline)
                remaining = max(.001, deadline - time.monotonic())
                request.extensions['timeout'] = {key: min(self.request_timeout_ms / 1000, remaining)
                                                 for key in ('connect', 'read', 'write', 'pool')}
            attempts += 1

        def stop_error_retry(response):
            if self.pacer is not None and response.status_code == 429:
                retry_after = response.headers.get('retry-after', '')
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 60
                self.pacer.cooldown(self.model, delay)
            # The installed SDK normalizes attempts=0 to 1 before its Interactions
            # adapter interprets that value as one retry. Fail at the public HTTP
            # hook boundary instead; leave legacy extraction retries unchanged.
            if self.request_retries == 0 and response.status_code >= 400:
                response.close()
                raise _StopHttpRetry(response.status_code)

        def failure(code, text):
            return ExtractionError(code, text, attempts=attempts)

        try:
            with genai.Client(api_key=self._api_key, vertexai=False, enterprise=False,
                              http_options={
                                  "timeout": self.request_timeout_ms,
                                  "client_args": {"event_hooks": {"request": [record_request],
                                                                  "response": [stop_error_retry]}},
                                  # In pinned google-genai 2.34.0, Interactions maps
                                  # attempts to RETRIES, unlike generateContent.
                                  # Two means three HTTP attempts; transport-tested.
                                  "retry_options": {"attempts": self.request_retries, "initial_delay": 2,
                                                    "max_delay": 4.5, "exp_base": 2, "jitter": 0.5,
                                                    "http_status_codes": [500, 502, 503, 504]},
                              }) as client:
                response = client.interactions.create(
                    model=self.model, input=message, system_instruction=self.system_instruction,
                    response_format={"type": "text", "mime_type": "application/json", "schema": self.response_schema.model_json_schema()},
                    generation_config={"thinking_level": "low", "max_output_tokens": 2048},
                    store=False, automatic_continuation=False,
                )
                return response, attempts
        except Exception as error:
            # The Interactions SDK has its own exception hierarchy. Contain all
            # SDK errors here so a URL, response body, or credential cannot leak.
            if isinstance(error, ExtractionError):
                raise
            status = getattr(error, "status_code", getattr(error, "code", None))
            if status == 429:
                raise failure("gemini_rate_limited", "Gemini quota reached. Wait before retrying.") from None
            if status in (401, 403):
                raise failure("gemini_access_denied", "Gemini rejected the key or model access.") from None
            if status in (400, 404):
                raise failure("gemini_request_rejected", "Gemini rejected the model or request configuration.") from None
            if status in (500, 502, 503, 504):
                raise failure("gemini_service_unavailable", "Gemini returned a server error. Retry later; no action was proposed.") from None
            raise failure("gemini_unavailable", "Gemini could not complete extraction. Check network access and retry later.") from None
