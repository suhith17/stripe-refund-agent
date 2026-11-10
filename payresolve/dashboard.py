"""Local operator screens that reuse the existing CLI orchestration."""

import json
import re
import secrets
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import Blueprint, abort, redirect, render_template, request, session, url_for, jsonify

from . import m2
from .execution import ExecutionStore
from .catalog import CatalogStore, last_week
from .cases import CaseStore, CaseService, now
from .agent import AgentRunner, GeminiPlanner
from .model_runtime import configured_planner
from .automation import CaseAutomation, TRANSIENT_MODEL
from .case_view import present_case
from .gemini_gateway import ExtractionError
from .stripe_gateway import SandboxError, StripeSandbox


def register_dashboard(app, settings):
    store = ExecutionStore(settings.database_path)
    catalog = CatalogStore(settings.database_path)
    cases = CaseStore(settings.database_path)
    automation = CaseAutomation(settings)  # Queue/read access only; CLI owns the worker.
    blueprint = Blueprint("dashboard", __name__)
    app.config.update(SECRET_KEY=secrets.token_bytes(32), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for secret in (settings.stripe_secret_key, settings.gemini_api_key):
                if secret:
                    value = value.replace(secret, "[redacted]")
            return re.sub(r"(?:[spr]k_(?:test|live)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{20,})", "[redacted]", value)
        return value

    @blueprint.before_request
    def local_form_boundary():
        if request.remote_addr not in ("127.0.0.1", "::1") or urlsplit(request.host_url).hostname not in ("localhost", "127.0.0.1", "::1"):
            abort(403)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST":
            if request.headers.get("Origin") not in (None, request.host_url.rstrip("/")):
                return render_template("dashboard/error.html", error="This form's origin could not be verified. Return to requests, reopen the request, and submit it again from this local workspace.", code="form_origin_rejected"), 403
            token = request.form.get("csrf", "")
            if not secrets.compare_digest(token.encode("utf-8"), session["csrf"].encode("utf-8")):
                return render_template("dashboard/error.html", error="This form has expired or its session cookie is missing. Return to requests and reopen the request before submitting again. Allow cookies for this local workspace.", code="form_session_expired"), 403

    @blueprint.after_request
    def private_page(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer makes native browser form POSTs send Origin: null,
        # conflicting with the same-origin guard above. Keep external referrers private.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    @blueprint.context_processor
    def context():
        return {"csrf": session["csrf"], "model_name": settings.gemini_model,
                "gemini_ready": bool(settings.gemini_api_key), "stripe_ready": bool(settings.stripe_secret_key)}

    @app.template_filter("money")
    def money(value):
        if type(value) is not int:
            return "—"
        return f"${value // 100:,}.{value % 100:02d}"

    @blueprint.get("/demo")
    def demo_purchases():
        customer_id = session.get("demo_customer_id")
        customer = catalog.customer(customer_id) if customer_id else None
        query = request.args.get("q", "")
        previous_week = request.args.get("last_week") == "yes"
        start, end = last_week() if previous_week else (None, None)
        orders = catalog.search(customer_id, query, purchased_from=start, purchased_to=end) if customer else []
        return render_template("dashboard/purchases.html", customers=catalog.customers(), customer=customer,
                               orders=orders, query=query, previous_week=previous_week,
                               cases=cases.list_cases(customer_id) if customer else [])

    @blueprint.post('/demo/cases')
    def create_case():
        result = cases.create_case(session.get('demo_customer_id'), request.form.get('message'),
                                   secrets=(settings.stripe_secret_key, settings.gemini_api_key))
        run_agent(session.get('demo_customer_id'), result['id'])
        return redirect(url_for('dashboard.case_detail', case_id=result['id']), code=303)

    def run_agent(customer_id, case_id):
        return AgentRunner(cases, StripeSandbox(settings.stripe_secret_key),
                           configured_planner(settings, GeminiPlanner),
                           tool_budget=settings.agent_tool_budget, context_mode=settings.agent_context_mode).run(customer_id, case_id)

    @blueprint.get('/demo/cases/<uuid:case_id>')
    def case_detail(case_id):
        customer_id = session.get('demo_customer_id')
        case = cases.snapshot(customer_id, str(case_id))
        jobs = automation.jobs(customer_id, str(case_id))
        pending = any(j['status'] in ('pending','running') for j in jobs)
        stopped = any(j['status'] in ('stopped','exhausted') for j in jobs)
        last_turn = case['agent_turns'][-1] if case['agent_turns'] else None
        failed_turn = last_turn and last_turn['status'] not in ('completed','running','superseded')
        automatic = pending or (not stopped and (automation.refund_eligible(case) or (failed_turn and last_turn['status'] in TRANSIENT_MODEL)))
        selected = case.get('remembered_details', {}).get('order', {}).get('order_id')
        if case['operation_id'] or case['stage'] == 'awaiting_confirmation':
            selected = case['selected_order']
        purchase = None
        if selected:
            purchase = {'order': catalog.order(customer_id, selected), 'remaining_minor': None, 'observed_at': None}
            for event in reversed(case['events']):
                detail = event['details']
                if detail.get('tool') == 'inspect_payment' and detail.get('order', {}).get('order_id') == selected and 'payment' in detail:
                    purchase.update(remaining_minor=detail['payment']['refundable_amount'], observed_at=event['created_at'])
                    break
                if event['kind'] == 'proposal_validated' and detail.get('order_id') == selected:
                    purchase.update(remaining_minor=detail['remaining_minor'], observed_at=detail['observed_at'])
                    break
            if case['operation_id'] and case['observed_at']:
                with cases.connect() as connection:
                    operation = cases.operation(connection, case['operation_id'])
                observed = json.loads(operation['result_json'] or '{}') if operation else {}
                if observed.get('payment', {}).get('id') == purchase['order']['payment_id']:
                    purchase.update(remaining_minor=observed['payment']['refundable_amount'], observed_at=case['observed_at'])
        return render_template('dashboard/case.html', case=redact(case), orders=catalog.search(customer_id),
                               related_cases=redact(cases.related_cases(customer_id, str(case_id))),
                               customer=catalog.customer(customer_id), customers=catalog.customers(),
                               cases=redact(cases.list_cases(customer_id)), purchase=redact(purchase),
                               view=redact(present_case(case)), jobs=jobs, automatic=automatic, retry_agent=failed_turn and not automatic,
                               manual_resume=case['operation_id'] and case['stage'] not in ('verified','failed') and not automatic)

    @blueprint.get('/demo/cases/<uuid:case_id>/state')
    def case_state(case_id):
        customer_id = session.get('demo_customer_id')
        case = cases.snapshot(customer_id, str(case_id))
        return jsonify(version=case['version'], stage=case['stage'], jobs=automation.jobs(customer_id,str(case_id)),
                       activity=present_case(case)['live_activity'])

    @blueprint.get('/demo/cases/<uuid:case_id>/record')
    def case_record(case_id):
        customer_id = session.get('demo_customer_id')
        case = cases.snapshot(customer_id, str(case_id))
        response = jsonify(redact({
            'schema_version': 'case-record-v1', 'exported_at': now(),
            'evidence_scope': 'Saved local observations; this download does not recheck providers.',
            'case': case, 'recovery_jobs': automation.jobs(customer_id, str(case_id)),
        }))
        response.headers['Content-Disposition'] = f'attachment; filename="payresolve-case-{case_id}.json"'
        return response

    @blueprint.post('/demo/cases/<uuid:case_id>/<action>')
    def case_action(case_id, action):
        case_id, customer_id = str(case_id), session.get('demo_customer_id')
        cases.snapshot(customer_id, case_id)  # Authorize before constructing a provider client.
        if action not in ('message', 'propose', 'confirm', 'cancel', 'resume', 'retry_agent'):
            abort(404)
        try:
            version = int(request.form.get('version', ''))
            if action == 'propose':
                raw_amount = request.form.get('amount', '')
                if not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', raw_amount):
                    abort(400)
                amount = int(Decimal(raw_amount) * 100)
            if action == 'confirm':
                revision = int(request.form.get('revision', ''))
        except ValueError:
            abort(400)
        if action == 'message':
            cases.message(customer_id, case_id, request.form.get('message'), version,
                          secrets=(settings.stripe_secret_key, settings.gemini_api_key), status_followup=True)
            run_agent(customer_id, case_id)
        elif action == 'retry_agent':
            if cases.snapshot(customer_id, case_id)['version'] != version:
                abort(409)
            run_agent(customer_id, case_id)
        elif action == 'cancel':
            cases.cancel(customer_id, case_id, version)
        else:
            service = CaseService(cases, StripeSandbox(settings.stripe_secret_key))
            if action == 'propose':
                service.propose(customer_id, case_id, request.form.get('order_id'), amount, 'usd', version)
            elif action == 'confirm':
                if request.form.get('confirm') != 'yes':
                    abort(400)
                service.confirm(customer_id, case_id, revision, version)
            else:
                service.resume(customer_id, case_id)
        return redirect(url_for('dashboard.case_detail', case_id=case_id), code=303)

    @blueprint.post("/demo/customer")
    def select_demo_customer():
        customer = catalog.customer(request.form.get("customer_id"))
        session["demo_customer_id"] = customer["id"]
        return redirect(url_for("dashboard.demo_purchases"), code=303)

    @blueprint.get("/demo/orders/<reference>")
    def demo_order(reference):
        order = catalog.order(session.get("demo_customer_id"), reference)
        return render_template("dashboard/purchase.html", order=order)

    @blueprint.get("/")
    def home():
        with store.connect() as connection:
            rows = connection.execute(
                "SELECT r.*, (SELECT a.result_json FROM analysis_runs a WHERE a.request_id=r.id "
                "ORDER BY a.created_at DESC,a.rowid DESC LIMIT 1) AS latest_json, "
                "(SELECT COALESCE(o.status,'approved') FROM refund_approvals p LEFT JOIN stripe_operations o "
                "ON o.operation_id=p.operation_id WHERE p.request_id=r.id) AS execution_status "
                "FROM refund_requests r WHERE NOT EXISTS (SELECT 1 FROM refund_cases c WHERE c.request_id=r.id) "
                "ORDER BY r.created_at DESC,r.rowid DESC LIMIT 30").fetchall()
        records = []
        for row in rows:
            item = dict(row)
            latest = json.loads(item.pop("latest_json")) if row["latest_json"] else {}
            item["stage"] = item.pop("execution_status") or latest.get("decision", {}).get("action") or latest.get("status", "received")
            records.append(item)
        return render_template("dashboard/home.html", records=redact(records), payments=redact(store.list_payments()))

    @blueprint.get("/ui/requests/<uuid:request_id>")
    def detail(request_id):
        history = store.history(str(request_id))
        with store.connect() as connection:
            approval = connection.execute("SELECT analysis_id FROM refund_approvals WHERE request_id=?", (str(request_id),)).fetchone()
        execution = store.execution(approval[0]) if approval else None
        return render_template("dashboard/detail.html", history=redact(history), execution=redact(execution))

    @blueprint.post("/ui/requests")
    def submit():
        result = m2.execute(SimpleNamespace(m2_command="analyze", message=request.form.get("message"), request_id=None), settings)
        return redirect(url_for("dashboard.detail", request_id=result["request_id"]), code=303)

    @blueprint.post("/ui/requests/<uuid:request_id>/analyze")
    def analyze(request_id):
        result = m2.execute(SimpleNamespace(m2_command="analyze", message=None, request_id=str(request_id)), settings)
        return redirect(url_for("dashboard.detail", request_id=result["request_id"]), code=303)

    @blueprint.post("/ui/analyses/<uuid:analysis_id>/<action>")
    def act(analysis_id, action):
        if action not in ("clarify", "retry", "approve", "execute", "verify"):
            abort(404)
        analysis = store.analysis(str(analysis_id))
        args = {"m2_command": action, "analysis_id": str(analysis_id)}
        if action == "clarify":
            args["message"] = request.form.get("message")
        elif action == "retry":
            if analysis["status"] != "failed":
                abort(409)
            if analysis.get("parent_analysis_id"):
                args.update(m2_command="clarify", analysis_id=analysis["parent_analysis_id"], message=analysis["input_message"])
            else:
                args.update(m2_command="analyze", request_id=analysis["request_id"], message=None)
        elif action == "approve":
            if request.form.get("confirm") != "yes":
                abort(400)
            try:
                amount = int(request.form.get("amount", ""))
            except ValueError:
                abort(400)
            args.update(payment_id=request.form.get("payment_id"), amount=amount, currency=request.form.get("currency"))
        m2.execute(SimpleNamespace(**args), settings)
        return redirect(url_for("dashboard.detail", request_id=analysis["request_id"]), code=303)

    @blueprint.errorhandler(ExtractionError)
    @blueprint.errorhandler(SandboxError)
    def workflow_error(error):
        status = 404 if error.code in ("analysis_not_found", "request_not_found") else 400 if isinstance(error, ExtractionError) else error.status
        return render_template("dashboard/error.html", error=redact(error.message), code=error.code), status

    @blueprint.errorhandler(sqlite3.Error)
    def storage_error(error):
        return render_template("dashboard/error.html", error="Storage is busy. Return to the request and retry the same action.", code="storage_unavailable"), 503

    app.register_blueprint(blueprint)
