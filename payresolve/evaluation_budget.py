"""Conservative, persistent HTTP request reservations for one fixed-model study."""

from contextlib import closing
import re
import sqlite3

from .gemini_gateway import ExtractionError

BUDGET_ERRORS = {'evaluation_budget_exhausted', 'evaluation_budget_unavailable'}


class EvaluationBudget:
    def __init__(self, path, model, limit):
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError('Choose a positive request cap of at most 100000.')
        if not isinstance(model, str) or not re.fullmatch(r'gemini-[A-Za-z0-9.-]+', model):
            raise ValueError('The request budget requires a fixed Gemini model.')
        self.path, self.model, self.limit = path, model, limit

    def initialize(self):
        # Exclusive file creation prevents an accidental reset of an existing run.
        with self.path.open('xb'):
            pass
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE budget (id INTEGER PRIMARY KEY CHECK(id=1), model TEXT, request_limit INTEGER)')
            connection.execute('INSERT INTO budget VALUES (1,?,?)', (self.model, self.limit))
            connection.execute('CREATE TABLE reservations (id INTEGER PRIMARY KEY, trial_id TEXT NOT NULL, '
                               'reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')

    def connect(self):
        # Never recreate a missing ledger in a worker or continuation.
        return sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)

    def _check(self, connection):
        if connection.execute('SELECT model,request_limit FROM budget WHERE id=1').fetchone() != (self.model, self.limit):
            raise ExtractionError('evaluation_budget_unavailable', 'The saved request budget does not match this study.', attempts=0)

    def snapshot(self):
        try:
            with closing(self.connect()) as connection:
                self._check(connection)
                used = connection.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]
            return {'model': self.model, 'limit': self.limit, 'reserved_requests': used,
                    'remaining': max(0, self.limit - used),
                    'accounting': 'Reservations include interrupted/unknown sends; never refunded or reset automatically.'}
        except sqlite3.Error:
            raise ExtractionError('evaluation_budget_unavailable', 'The request budget ledger is unavailable.', attempts=0) from None

    def reserve(self, model, trial_id):
        if model != self.model:
            raise ExtractionError('evaluation_budget_unavailable', 'A different model cannot use this study budget.', attempts=0)
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                self._check(connection)
                used = connection.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]
                if used >= self.limit:
                    raise ExtractionError('evaluation_budget_exhausted', 'The study request cap has been reached.', attempts=0)
                connection.execute('INSERT INTO reservations (trial_id) VALUES (?)', (trial_id,))
        except sqlite3.Error:
            raise ExtractionError('evaluation_budget_unavailable', 'The request budget ledger is unavailable.', attempts=0) from None


class BudgetedPacer:
    """Called by the existing SDK HTTP request hook, immediately before sending."""
    def __init__(self, pacer, budget, trial_id):
        self.pacer, self.budget, self.trial_id = pacer, budget, trial_id

    def wait(self, model, deadline):
        if not self.budget.snapshot()['remaining']:
            raise ExtractionError('evaluation_budget_exhausted', 'The study request cap has been reached.', attempts=0)
        waited = self.pacer.wait(model, deadline)
        self.budget.reserve(model, self.trial_id)
        return waited

    def cooldown(self, model, delay):
        self.pacer.cooldown(model, delay)


def study_budget(runs_root, specification):
    if (not isinstance(specification, dict) or set(specification) != {'owner_run_id', 'model', 'limit'}
            or not isinstance(specification['owner_run_id'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', specification['owner_run_id'])):
        raise ValueError('Invalid study budget reference.')
    return EvaluationBudget(runs_root / specification['owner_run_id'] / 'request-budget.db',
                            specification['model'], specification['limit'])


def budget_observation(budget):
    """Reports retain diagnostics even when the ledger cannot be read."""
    try:
        return budget.snapshot()
    except ExtractionError as error:
        return {'error': error.code, 'remaining': 0, 'reserved_requests': None}
