"""Analysis stays read-only; separate explicit approval/execution commands."""

from .analysis import AnalysisService, AnalysisStore, DryRunPolicy
from .gemini_gateway import GeminiExtractor
from .fallback import FallbackExtractor
from .stripe_gateway import StripeSandbox
from .execution import ExecutionStore, ReviewedExecution


def add_parser(subparsers):
    parser = subparsers.add_parser("m2", help="Refund analysis, explicit approval, and sandbox execution")
    commands = parser.add_subparsers(dest="m2_command", required=True)
    analyze = commands.add_parser("analyze", help="Analyze synthetic refund text without executing a refund")
    source = analyze.add_mutually_exclusive_group(required=True)
    source.add_argument("--message")
    source.add_argument("--request-id")
    show = commands.add_parser("show", help="Read a saved analysis without contacting either API")
    show.add_argument("run_id")
    clarify = commands.add_parser("clarify", help="Answer a clarify decision with a complete corrected request; dry run only")
    clarify.add_argument("analysis_id")
    clarify.add_argument("--message", required=True)
    history = commands.add_parser("history", help="Read the original request and all analyses locally")
    history.add_argument("request_id")
    evaluate = commands.add_parser("eval", help="Evaluate Gemini on labeled synthetic cases; no Stripe calls")
    evaluate.add_argument("--limit", type=int, default=2, help="1 to 20 cases; start with two")
    evaluate.add_argument("--interval", type=float, default=6, help="Seconds between calls (0 to 60)")
    approve = commands.add_parser("approve", help="Save approval of the exact proposal; no API calls")
    approve.add_argument("analysis_id")
    approve.add_argument("--payment-id", required=True)
    approve.add_argument("--amount", required=True, type=int, help="Exact proposed amount in USD cents")
    approve.add_argument("--currency", required=True, choices=["usd"])
    for name, help_text in [("execute", "Execute an approved sandbox refund; retry with the same analysis ID"),
                            ("execution", "Inspect approval and execution status locally"),
                            ("verify", "Retrieve final refund/payment state without submitting a refund")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("analysis_id")


def execute(args, settings):
    if args.m2_command == "show":
        return AnalysisStore(settings.database_path).analysis(args.run_id)
    if args.m2_command == "history":
        return AnalysisStore(settings.database_path).history(args.request_id)
    if args.m2_command in ("approve", "execute", "execution", "verify"):
        store = ExecutionStore(settings.database_path)
        if args.m2_command == "approve":
            store.approve(args.analysis_id, args.payment_id, args.amount, args.currency)
            return store.execution(args.analysis_id)
        if args.m2_command == "execution":
            return store.execution(args.analysis_id)
        return ReviewedExecution(store, StripeSandbox(settings.stripe_secret_key)).execute(
            args.analysis_id, verify_only=args.m2_command == "verify",
        )
    extractor = GeminiExtractor(settings.gemini_api_key, settings.gemini_model,
                                forbidden_values=(settings.stripe_secret_key,))
    if args.m2_command == "eval":
        from .evaluation import evaluate
        return evaluate(extractor, limit=args.limit, interval=args.interval)
    if settings.gemini_fallback_model and settings.gemini_fallback_model != settings.gemini_model:
        fallback = GeminiExtractor(settings.gemini_api_key, settings.gemini_fallback_model,
                                   forbidden_values=(settings.stripe_secret_key,))
        extractor = FallbackExtractor(extractor, fallback)
    store = AnalysisStore(settings.database_path)
    gateway = StripeSandbox(settings.stripe_secret_key)
    service = AnalysisService(store, extractor, DryRunPolicy(store, gateway.get_payment))
    if args.m2_command == "clarify":
        return service.clarify(args.analysis_id, args.message)
    return service.analyze(
        message=args.message, request_id=args.request_id,
    )
