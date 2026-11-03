"""Availability fallback for dry-run analysis; evaluations stay single-model."""

import time

from .gemini_gateway import ExtractionError

AVAILABILITY_ERRORS = {"gemini_service_unavailable", "gemini_unavailable"}


class FallbackExtractor:
    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.model = primary.model

    def validate(self, message):
        return self.primary.validate(message)

    def extract(self, message):
        message = self.validate(message)
        started = time.perf_counter()
        history = []
        for index, extractor in enumerate((self.primary, self.fallback)):
            try:
                result = extractor.extract(message)
            except ExtractionError as error:
                history.append({"model": extractor.model, "attempts": error.attempts,
                                "latency_ms": error.latency_ms, "error_code": error.code})
                if index == 0 and error.code in AVAILABILITY_ERRORS:
                    continue
                error.attempts = sum(item["attempts"] for item in history)
                error.latency_ms = round((time.perf_counter() - started) * 1000)
                error.attempt_history = history
                raise
            history.append({"model": extractor.model, "attempts": result["attempts"],
                            "latency_ms": result["latency_ms"], "status": "completed"})
            return result | {"requested_model": self.model, "fallback_used": index == 1,
                             "attempts": sum(item["attempts"] for item in history),
                             "latency_ms": round((time.perf_counter() - started) * 1000),
                             "attempt_history": history}
