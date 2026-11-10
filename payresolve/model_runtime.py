"""Local, cross-process pacing and bounded chat availability fallback."""

import sqlite3
import time
from contextlib import closing

from .gemini_gateway import ExtractionError

AVAILABILITY_ERRORS = {'gemini_unavailable', 'gemini_service_unavailable', 'gemini_rate_limited'}


class RequestPacer:
    """Space request starts across workers; never hold a lock while waiting.

    The repository-wide RPM budget is shared by all models. Other apps and
    token/day quotas are outside its scope. Store only model names and timing.
    """

    def __init__(self, path, rpm=15, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        if type(rpm) is not int or not 1 <= rpm <= 60:
            raise ValueError('RPM must be an integer from 1 to 60.')
        self.path, self.interval = path, 60 / rpm + .05
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS model_pacing (name TEXT PRIMARY KEY, until REAL NOT NULL)')

    def wait(self, model, deadline):
        try:
            return self._wait(model, deadline)
        except sqlite3.Error:
            raise ExtractionError('gemini_pacing_unavailable', 'The local request limiter is unavailable.') from None

    def _wait(self, model, deadline):
        started = self.monotonic()
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= .05:
                raise ExtractionError('gemini_pacing_timeout', 'The turn budget expired while waiting for model capacity.')
            with closing(sqlite3.connect(self.path, timeout=min(1, remaining))) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                if deadline - self.monotonic() <= .05:
                    raise ExtractionError('gemini_pacing_timeout', 'The turn budget expired waiting for model capacity.')
                times = dict(connection.execute('SELECT name, until FROM model_pacing'))
                now = self.clock()
                if times.get(model, 0) > now:
                    raise ExtractionError('gemini_rate_limited', 'This model is cooling down after a quota response.')
                delay = max(0, times.get('*', 0) - now)
                if delay == 0:
                    connection.execute('INSERT OR REPLACE INTO model_pacing VALUES (?, ?)', ('*', now + self.interval))
                    return round((self.monotonic() - started) * 1000)
            if delay + .05 >= deadline - self.monotonic():
                raise ExtractionError('gemini_pacing_timeout', 'The turn budget cannot accommodate the next paced request.')
            self.sleep(delay)

    def cooldown(self, model, seconds):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO model_pacing VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET until=max(until, excluded.until)',
                               (model, self.clock() + max(1, seconds)))


class ChatPlanner:
    """One availability switch per turn; subsequent steps stay on the backup."""

    def __init__(self, primary, backup=None):
        self.primary, self.backup = primary, backup
        self.model = primary.model
        self.begin_turn()

    def begin_turn(self):
        self.active = self.primary

    def step(self, context, *, remaining_seconds):
        started = time.monotonic()
        records = []
        while True:
            remaining = remaining_seconds - (time.monotonic() - started)
            try:
                response = self.active.step(context, remaining_seconds=max(.001, remaining))
            except ExtractionError as error:
                records.append({'model': self.active.model, 'status': error.code,
                                'attempts': error.attempts, 'latency_ms': error.latency_ms, 'usage': None})
                if (self.active is self.primary and self.backup is not None and
                        error.code in AVAILABILITY_ERRORS and remaining_seconds - (time.monotonic() - started) > 1):
                    self.active = self.backup
                    continue
                error.model_attempts = records
                error.attempts = sum(r['attempts'] or 0 for r in records)
                error.latency_ms = round((time.monotonic() - started) * 1000)
                raise
            records.append({'model': self.active.model, 'status': 'completed',
                            'attempts': response.get('attempts'), 'latency_ms': response.get('latency_ms'),
                            'usage': response.get('usage')})
            return {**response, 'model': self.active.model, 'model_attempts': records,
                    'attempts': sum(r['attempts'] or 0 for r in records),
                    'latency_ms': round((time.monotonic() - started) * 1000),
                    'usage_complete': len(records) == 1}


def configured_planner(settings, planner_type, *, evaluation=False, model=None):
    pacer = RequestPacer(settings.model_pacing_path, settings.gemini_requests_per_minute)
    primary = planner_type(settings.gemini_api_key, model or settings.gemini_model,
                           forbidden_values=(settings.stripe_secret_key,), pacer=pacer)
    if evaluation:
        return primary  # Fixed-model comparisons must never silently switch.
    backup = None
    if settings.agent_fallback_model and settings.agent_fallback_model != settings.gemini_model:
        backup = planner_type(settings.gemini_api_key, settings.agent_fallback_model,
                              forbidden_values=(settings.stripe_secret_key,), pacer=pacer)
    return ChatPlanner(primary, backup)
