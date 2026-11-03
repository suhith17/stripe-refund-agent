"""One bounded, stateless Gemini extraction; no Stripe tools or credentials."""

import re
import time

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


def validate_message(message, secrets=()):
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000 or "\x00" in message:
        raise ExtractionError("invalid_message", "Provide 1 to 4000 characters without null bytes.")
    if any(secret and secret in message for secret in secrets) or re.search(
        r"(?:[spr]k_(?:test|live)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{20,})", message,
    ):
        raise ExtractionError("credential_in_message", "Remove credentials from the request text.")
    return message.strip()


class GeminiExtractor:
    def __init__(self, api_key, model, *, forbidden_values=()):
        self._api_key = api_key
        self.model = model
        self._secrets = (api_key, *forbidden_values)
        if not re.fullmatch(r"gemini-[A-Za-z0-9.-]+", model):
            raise ExtractionError("invalid_gemini_model", "Set GEMINI_MODEL to a Gemini model identifier.")

    def validate(self, message):
        return validate_message(message, self._secrets)

    def extract(self, message):
        message = self.validate(message)
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
            intent = Intent.model_validate_json(response.output_text)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise ExtractionError("invalid_model_output", "Gemini returned an invalid extraction; no action was proposed.",
                                  attempts=attempt, latency_ms=elapsed) from None
        # Do not persist a model response that somehow repeats configured secrets.
        if any(secret and secret in intent.model_dump_json() for secret in self._secrets):
            raise ExtractionError("invalid_model_output", "Gemini returned an invalid extraction; no action was proposed.",
                                  attempts=attempt, latency_ms=elapsed)
        usage = getattr(response, "usage", None)
        return {
            "intent": intent.model_dump(), "model": self.model, "prompt_version": PROMPT_VERSION,
            "attempts": attempt, "latency_ms": elapsed,
            "usage": {name: getattr(usage, name, None) for name in
                      ("total_input_tokens", "total_output_tokens", "total_thought_tokens", "total_tokens")},
        }

    def _request(self, message):
        attempts = 0

        def record_request(request):
            nonlocal attempts
            attempts += 1

        def failure(code, text):
            return ExtractionError(code, text, attempts=attempts)

        try:
            with genai.Client(api_key=self._api_key, vertexai=False, enterprise=False,
                              http_options={
                                  "timeout": 30000,
                                  "client_args": {"event_hooks": {"request": [record_request]}},
                                  # In pinned google-genai 2.34.0, Interactions maps
                                  # attempts to RETRIES, unlike generateContent.
                                  # Two means three HTTP attempts; transport-tested.
                                  "retry_options": {"attempts": 2, "initial_delay": 2,
                                                    "max_delay": 4.5, "exp_base": 2, "jitter": 0.5,
                                                    "http_status_codes": [500, 502, 503, 504]},
                              }) as client:
                response = client.interactions.create(
                    model=self.model, input=message, system_instruction=SYSTEM_INSTRUCTION,
                    response_format={"type": "text", "mime_type": "application/json", "schema": Intent.model_json_schema()},
                    generation_config={"thinking_level": "low", "max_output_tokens": 2048},
                    store=False, automatic_continuation=False,
                )
                return response, attempts
        except Exception as error:
            # The Interactions SDK has its own exception hierarchy. Contain all
            # SDK errors here so a URL, response body, or credential cannot leak.
            status = getattr(error, "status_code", getattr(error, "code", None))
            if status == 429:
                raise failure("gemini_rate_limited", "Gemini quota reached. Wait before retrying; no fallback model was used.") from None
            if status in (401, 403):
                raise failure("gemini_access_denied", "Gemini rejected the key or model access.") from None
            if status in (400, 404):
                raise failure("gemini_request_rejected", "Gemini rejected the model or request configuration.") from None
            if status in (500, 502, 503, 504):
                raise failure("gemini_service_unavailable", "Gemini returned a server error. Retry later; no action was proposed.") from None
            raise failure("gemini_unavailable", "Gemini could not complete extraction. Check network access and retry later.") from None
