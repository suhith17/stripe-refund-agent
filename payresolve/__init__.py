"""Local PayResolve backend."""

import sqlite3

from flask import Flask, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from .config import Settings
from .storage import RequestStore
from .stripe_gateway import SandboxError, StripeSandbox


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings if settings is not None else Settings.from_environment()
    sandbox = StripeSandbox(settings.stripe_secret_key)
    store = RequestStore(settings.database_path)
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, DEBUG=False)

    @app.get("/stripe/status")
    def stripe_status():
        return jsonify(sandbox.check_connection())

    @app.errorhandler(SandboxError)
    def sandbox_error(error):
        return jsonify(error={"code": error.code, "message": error.message}), error.status

    @app.get("/health")
    def health():
        store.check()
        return jsonify(status="ok", database="ok")

    @app.post("/requests")
    def create_request():
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"message"}:
            return jsonify(error={"code": "invalid_request",
                                  "message": "Provide a JSON object with only a message field."}), 400
        message = body["message"]
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000 or "\x00" in message:
            return jsonify(error={"code": "invalid_message",
                                  "message": "message must contain 1 to 4000 characters and no null bytes."}), 400
        record = store.create(message.strip())
        return jsonify(record), 201, {"Location": url_for("get_request", request_id=record["id"])}

    @app.get("/requests/<uuid:request_id>")
    def get_request(request_id):
        record = store.get(str(request_id))
        if record is None:
            return jsonify(error={"code": "not_found", "message": "Request not found."}), 404
        return jsonify(record)

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        # Do not send SQL, request text, database paths or exception details to clients.
        return jsonify(error={"code": "storage_unavailable",
                              "message": "Request storage is temporarily unavailable."}), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={"code": error.name.lower().replace(" ", "_"),
                              "message": error.name}), error.code

    return app
