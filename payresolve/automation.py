"""Durable, bounded background recovery for already-authorized case work."""

import sqlite3
import threading
import time

from .agent import AgentRunner, GeminiPlanner
from .model_runtime import configured_planner
from .cases import CaseStore, CaseService
from .gemini_gateway import ExtractionError
from .stripe_gateway import SandboxError, StripeSandbox

TRANSIENT_MODEL = {'gemini_unavailable', 'gemini_service_unavailable', 'gemini_incomplete'}
TRANSIENT_STRIPE = {'stripe_connection_failed', 'stripe_unavailable', 'stripe_rate_limited'}


class CaseAutomation:
    def __init__(self, settings, *, gateway_factory=None, planner_factory=None, clock=time.time):
        self.settings = settings
        self.store = CaseStore(settings.database_path)
        self.gateway_factory = gateway_factory or (lambda: StripeSandbox(settings.stripe_secret_key))
        self.planner_factory = planner_factory or (lambda: configured_planner(settings, GeminiPlanner))
        self.clock = clock
        self.stopped = threading.Event()
        self.thread = None
        with self.store.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS case_jobs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                kind TEXT NOT NULL, message_id TEXT, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                due_at REAL NOT NULL, lease_until REAL, last_error TEXT)""")

    @staticmethod
    def refund_eligible(case):
        return bool(case['operation_id']) and (
            case['stage'] in ('processing', 'pending') or
            (case['stage'] == 'needs_reconciliation' and case['last_error'] in TRANSIENT_STRIPE))

    def agent_eligible(self, case, turn):
        if not turn:
            return False
        return ((turn['status'] in TRANSIENT_MODEL and case['version']==turn['case_version']+1) or
                (turn['status']=='running' and turn['expires_at']<=self.clock() and case['version']==turn['case_version']))

    def discover(self):
        with self.store.locked() as c:
            for case in c.execute('SELECT * FROM refund_cases').fetchall():
                if self.refund_eligible(case):
                    c.execute("INSERT OR IGNORE INTO case_jobs(id,case_id,customer_id,kind,status,max_attempts,due_at) VALUES (?,?,?,'refund','pending',3,?)",
                              ('refund:'+case['operation_id'], case['id'],case['customer_id'],self.clock()+5))
                if case['stage'] in ('cancelled','awaiting_confirmation'):
                    continue
                turn=c.execute('SELECT * FROM agent_turns WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(case['id'],)).fetchone()
                message=c.execute("SELECT id FROM case_messages WHERE case_id=? AND role='customer' ORDER BY rowid DESC LIMIT 1",(case['id'],)).fetchone()
                # Retry one transient failure for this exact customer message.
                # The retry's new turn ID must not reset this durable budget.
                if message and self.agent_eligible(case,turn):
                    c.execute("INSERT OR IGNORE INTO case_jobs(id,case_id,customer_id,kind,message_id,status,max_attempts,due_at) VALUES (?,?,?,'agent',?,'pending',1,?)",
                              ('agent:'+message['id'],case['id'],case['customer_id'],message['id'],self.clock()+10))

    def tick(self):
        self.discover()
        with self.store.locked() as c:
            c.execute("UPDATE case_jobs SET status=CASE WHEN attempts<max_attempts THEN 'pending' ELSE 'exhausted' END WHERE status='running' AND lease_until<=?",(self.clock(),))
            job=c.execute("SELECT * FROM case_jobs WHERE status='pending' AND due_at<=? ORDER BY due_at,id LIMIT 1",(self.clock(),)).fetchone()
            if not job:
                return False
            job=dict(job)
            c.execute("UPDATE case_jobs SET status='running',attempts=attempts+1,lease_until=? WHERE id=?",(self.clock()+180,job['id']))
            self.store.event(c,job['case_id'],'automatic_recovery_started',{'job_id':job['id'],'kind':job['kind'],'attempt':job['attempts']+1})
        error=None
        status='done'
        try:
            case=self.store.snapshot(job['customer_id'],job['case_id'])
            if job['kind']=='refund':
                if self.refund_eligible(case):
                    case=CaseService(self.store,self.gateway_factory()).resume(job['customer_id'],job['case_id'])
                    error=case['last_error']
                    if self.refund_eligible(case):
                        status='pending' if job['attempts']+1<job['max_attempts'] else 'exhausted'
                    elif case['stage']=='needs_reconciliation':
                        status='stopped'
            else:
                latest=next((m['id'] for m in reversed(case['messages']) if m['role']=='customer'),None)
                turn=case['agent_turns'][-1] if case['agent_turns'] else None
                if (latest!=job['message_id'] or case['stage'] in ('cancelled','awaiting_confirmation') or not turn
                        or not self.agent_eligible(case,turn)):
                    status='superseded'
                else:
                    case=AgentRunner(self.store,self.gateway_factory(),self.planner_factory(),
                                     tool_budget=self.settings.agent_tool_budget,context_mode=self.settings.agent_context_mode).run(job['customer_id'],job['case_id'],expected_version=case['version'])
                    result=case['agent_turns'][-1]['status']
                    if result!='completed':
                        status,error='exhausted',result
        except (SandboxError, ExtractionError) as caught:
            status,error='stopped',caught.code
        with self.store.locked() as c:
            changed=c.execute("UPDATE case_jobs SET status=?,due_at=?,lease_until=NULL,last_error=? WHERE id=? AND status='running' AND attempts=?",
                              (status,self.clock()+15*(3**job['attempts']),error,job['id'],job['attempts']+1)).rowcount
            if changed:
                self.store.event(c,job['case_id'],'automatic_recovery_finished',{'job_id':job['id'],'status':status,'error':error})
                if status in ('exhausted','stopped'):
                    self.store.add_message(c,job['case_id'],'agent',
                        'Automatic checks have paused. Your case and existing operation are preserved. Review the current status before trying again; no new refund was authorized.')
                    c.execute('UPDATE refund_cases SET version=version+1 WHERE id=?',(job['case_id'],))
        return True

    def jobs(self, customer_id, case_id):
        with self.store.connect() as c:
            self.store.owned(c,customer_id,case_id)
            return [dict(r) for r in c.execute('SELECT kind,status,attempts,max_attempts,last_error FROM case_jobs WHERE case_id=? ORDER BY rowid',(case_id,))]

    def start(self):
        def work():
            while not self.stopped.is_set():
                try:
                    self.tick()
                except sqlite3.Error:
                    # SQLite contention is retried by the next bounded job lease;
                    # never print database paths or provider details.
                    pass
                self.stopped.wait(2)
        self.thread=threading.Thread(target=work,name='payresolve-case-recovery',daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
