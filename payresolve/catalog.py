"""Customer-scoped demo orders. Customer identity is supplied by the server."""

from datetime import date, timedelta
import re

from .operations import OperationStore
from .stripe_gateway import SandboxError, StripeSandbox
from .workflow import SandboxWorkflow, reject

DEMO_DATE = date(2026, 11, 5)
FIXTURES = (
    ("ORD-101", "a", "Wireless headphones", "2026-10-26", 3999),
    ("ORD-102", "a", "Wireless headphones", "2026-10-30", 5999),
    ("ORD-201", "b", "Wireless headphones", "2026-10-30", 7999),
)


def validate_namespace(namespace):
    if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", namespace):
        reject("invalid_demo_namespace", "Use 1 to 32 letters, digits, underscores or hyphens.")
    return namespace


def last_week(as_of=DEMO_DATE):
    monday = as_of - timedelta(days=as_of.weekday())
    return (monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat()


class CatalogStore(OperationStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS demo_catalogs (
                namespace TEXT PRIMARY KEY, scenario_date TEXT NOT NULL,
                fixture_version TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS demo_customers (
                id TEXT PRIMARY KEY, namespace TEXT NOT NULL, name TEXT NOT NULL,
                UNIQUE(namespace, name))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS customer_orders (
                namespace TEXT NOT NULL, order_id TEXT NOT NULL,
                customer_id TEXT NOT NULL, description TEXT NOT NULL,
                purchased_on TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                currency TEXT NOT NULL CHECK(currency = 'usd'),
                payment_id TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(namespace, order_id))""")
            connection.execute("CREATE INDEX IF NOT EXISTS orders_customer_date ON customer_orders(customer_id, purchased_on)")

    def customers(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT c.*, d.scenario_date FROM demo_customers c JOIN demo_catalogs d "
                "ON d.namespace=c.namespace ORDER BY c.namespace,c.id")]

    def customer(self, customer_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT c.*, d.scenario_date FROM demo_customers c JOIN demo_catalogs d "
                "ON d.namespace=c.namespace WHERE c.id=?", (customer_id,)).fetchone()
        if row is None:
            raise SandboxError("demo_customer_required", "Select a demo customer first.", 403)
        return dict(row)

    def order(self, customer_id, reference):
        self.customer(customer_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM customer_orders WHERE customer_id=? AND (order_id=? OR payment_id=?)",
                (customer_id, reference, reference)).fetchone()
        if row is None:
            # Missing and another customer's IDs deliberately have the same result.
            raise SandboxError("order_not_found", "No matching purchase was found for this demo customer.", 404)
        return dict(row)

    def search(self, customer_id, query="", *, purchased_from=None, purchased_to=None):
        self.customer(customer_id)
        if not isinstance(query, str) or len(query) > 500:
            reject("invalid_search", "Use a search phrase of at most 500 characters.")
        for value in (purchased_from, purchased_to):
            if value is not None:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except (ValueError, TypeError):
                    reject("invalid_search_dates", "Use dates formatted YYYY-MM-DD.")
        if purchased_from and purchased_to and purchased_from > purchased_to:
            reject("invalid_search_dates", "The start date must not follow the end date.")
        # Scope in SQL before matching. Never rank or expose other customers' rows.
        sql = "SELECT * FROM customer_orders WHERE customer_id=?"
        params = [customer_id]
        if purchased_from:
            sql += " AND purchased_on>=?"
            params.append(purchased_from)
        if purchased_to:
            sql += " AND purchased_on<=?"
            params.append(purchased_to)
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(sql + " ORDER BY purchased_on,order_id", params)]
        references = re.findall(r"\b(?:ORD-[A-Za-z0-9_-]+|pi_[A-Za-z0-9]+)\b", query)
        if references:
            return [row for row in rows if row["order_id"] in references or row["payment_id"] in references]
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        return [row for row in rows if tokens <= set(re.findall(r"[a-z0-9]+", row["description"].lower()))]

    def inspect_payment(self, customer_id, reference, gateway):
        order = self.order(customer_id, reference)  # Authorization before provider access.
        payment = gateway.get_payment(order["payment_id"])
        with self.connect() as connection:
            operation = self.operation(connection, order["operation_id"])
        if (not operation or payment["id"] != order["payment_id"]
                or payment["livemode"] is not False
                or payment["operation_key"] != operation["stripe_key"]
                or payment["amount"] != order["amount_minor"] or payment["currency"] != order["currency"]):
            reject("payment_identity_mismatch", "The payment does not match this purchase.")
        with self.connect() as connection:
            self.save_payment(connection, order["operation_id"], payment)
        return {"order": order, "payment": payment}


def seed_catalog(store, gateway, namespace):
    """Explicit operator setup; stable operation IDs recover interrupted seeds."""
    validate_namespace(namespace)
    with store.locked() as connection:
        connection.execute("INSERT OR IGNORE INTO demo_catalogs VALUES (?, ?, ?)",
                           (namespace, DEMO_DATE.isoformat(), "headphones-v1"))
        current = connection.execute("SELECT * FROM demo_catalogs WHERE namespace=?", (namespace,)).fetchone()
        if (current["scenario_date"], current["fixture_version"]) != (DEMO_DATE.isoformat(), "headphones-v1"):
            reject("demo_catalog_conflict", "This namespace belongs to a different fixture version.")
        for suffix, name in (("a", "Demo customer A"), ("b", "Demo customer B")):
            connection.execute("INSERT OR IGNORE INTO demo_customers VALUES (?, ?, ?)",
                               (f"{namespace}:{suffix}", namespace, name))
    workflow = SandboxWorkflow(store, gateway)
    for order_id, suffix, description, purchased_on, amount in FIXTURES:
        operation_id = f"catalog-v1-{namespace}-{order_id}"
        result = workflow.create_payment(operation_id, amount)
        if result["status"] != "verified":
            reject("demo_seed_incomplete", "A fixture payment is not verified. Retry the same namespace.")
        values = (namespace, order_id, f"{namespace}:{suffix}", description, purchased_on,
                  amount, "usd", result["payment"]["id"], operation_id)
        with store.locked() as connection:
            existing = connection.execute("SELECT * FROM customer_orders WHERE namespace=? AND order_id=?",
                                          (namespace, order_id)).fetchone()
            if existing:
                if tuple(existing) != values:
                    reject("demo_order_conflict", "A saved order differs from this fixture. It was not reassigned.")
            else:
                connection.execute("INSERT INTO customer_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    return {"status": "ready", "namespace": namespace, "scenario_date": DEMO_DATE.isoformat(),
            "customers": [c for c in store.customers() if c["namespace"] == namespace],
            "orders": store.search(f"{namespace}:a") + store.search(f"{namespace}:b"),
            "note": "Catalog dates are synthetic. Reseeding does not reset refunded balances."}


def add_parser(subparsers):
    parser = subparsers.add_parser("demo", help="Customer-scoped demo purchase catalog")
    commands = parser.add_subparsers(dest="demo_command", required=True)
    seed = commands.add_parser("seed", help="Create three dedicated Stripe sandbox payments; never refunds")
    seed.add_argument("--namespace", required=True, help="Reuse on retry; a new namespace creates new payments")
    extra = commands.add_parser("add-customer", help="Add customer C with two sandbox purchases; preserve existing balances")
    extra.add_argument("--namespace", required=True, help="Existing or new demo namespace; never an evaluation fixture")
    commands.add_parser("customers", help="List seeded demo identities locally")
    search = commands.add_parser("search", help="Search purchases for one demo customer; no API calls")
    search.add_argument("--customer", required=True)
    search.add_argument("--query", default="")
    search.add_argument("--last-week", action="store_true", help="Use the fixed demo clock, 2026-11-05")


def execute(args, settings):
    if args.demo_command in ("seed", "add-customer"):
        validate_namespace(args.namespace)
    store = CatalogStore(settings.database_path)
    if args.demo_command == "seed":
        return seed_catalog(store, StripeSandbox(settings.stripe_secret_key), args.namespace)
    if args.demo_command == "add-customer":
        return add_demo_customer(store, StripeSandbox(settings.stripe_secret_key), args.namespace)
    if args.demo_command == "customers":
        return {"customers": store.customers(), "identity_mode": "local_demo_not_authentication"}
    start, end = last_week() if args.last_week else (None, None)
    return {"orders": store.search(args.customer, args.query, purchased_from=start, purchased_to=end),
            "source": "local_catalog", "scenario_date": DEMO_DATE.isoformat()}


def add_demo_customer(store, gateway, namespace):
    """Opt-in demo expansion. Default benchmark seeding remains byte-for-byte data compatible."""
    seed_catalog(store, gateway, namespace)
    with store.locked() as connection:
        connection.execute('INSERT OR IGNORE INTO demo_customers VALUES (?,?,?)',
                           (f'{namespace}:c', namespace, 'Demo customer C'))
    workflow = SandboxWorkflow(store, gateway)
    for order_id, description, purchased_on, amount in (
            ('ORD-301', 'Mechanical keyboard', '2026-10-28', 8999),
            ('ORD-302', 'USB-C charger', '2026-10-31', 2499)):
        operation_id = f'catalog-v1-{namespace}-{order_id}'
        result = workflow.create_payment(operation_id, amount)
        if result['status'] != 'verified':
            reject('demo_seed_incomplete', 'Retry adding the customer with the same namespace.')
        values = (namespace, order_id, f'{namespace}:c', description, purchased_on,
                  amount, 'usd', result['payment']['id'], operation_id)
        with store.locked() as connection:
            existing = connection.execute('SELECT * FROM customer_orders WHERE namespace=? AND order_id=?',
                                          (namespace, order_id)).fetchone()
            if existing and tuple(existing) != values:
                reject('demo_order_conflict', 'A saved order differs from this fixture. It was not reassigned.')
            connection.execute('INSERT OR IGNORE INTO customer_orders VALUES (?,?,?,?,?,?,?,?,?)', values)
    return {'status': 'ready', 'namespace': namespace, 'customer': store.customer(f'{namespace}:c'),
            'orders': store.search(f'{namespace}:c'),
            'note': 'Existing purchases and refunded balances are preserved. A new namespace creates fresh sandbox payments.'}
