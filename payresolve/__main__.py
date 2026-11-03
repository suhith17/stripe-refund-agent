import argparse
import json
import sqlite3
import sys

from . import create_app
from .config import Settings
from .stripe_gateway import SandboxError, StripeSandbox
from . import m1
from . import m2
from .gemini_gateway import ExtractionError


def main() -> int:
    parser = argparse.ArgumentParser(description="Local PayResolve backend")
    parser.add_argument("--check-stripe", action="store_true", help="Read-only sandbox connection check")
    commands = parser.add_subparsers(dest="command")
    m1.add_parser(commands)
    m2.add_parser(commands)
    args = parser.parse_args()
    if args.check_stripe and args.command:
        parser.error("--check-stripe cannot be combined with a command")
    settings = Settings.from_environment()
    try:
        if args.command in ("m1", "m2"):
            result = (m1 if args.command == "m1" else m2).execute(args, settings)
            print(json.dumps(result, indent=2))
            if result.get("status") == "pending":
                return 2
            if result.get("status") in ("failed", "unresolved", "incomplete"):
                return 1
            if args.command == "m2" and args.m2_command == "eval" and result["passed"] != result["requested_cases"]:
                return 1
        elif args.check_stripe:
            print(json.dumps(StripeSandbox(settings.stripe_secret_key).check_connection()))
        else:
            create_app(settings).run(host="127.0.0.1", port=8000, debug=False, use_reloader=False)
    except (SandboxError, ExtractionError) as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}), file=sys.stderr)
        return 1
    except sqlite3.Error:
        print(json.dumps({"error": {"code": "storage_unavailable", "message": "SQLite is unavailable or busy. Retry the same operation ID."}}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
