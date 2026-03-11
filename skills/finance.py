"""Finance tracking skill — income/expense tracker with live exchange rates."""

DESCRIPTION = "Track income, expenses, balance, and generate reports"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_transaction",
            "description": "Add income or expense. Example: add_transaction(name='Lunch', amount=5000, currency='ARS', category='Food')",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Transaction description"},
                    "amount": {"type": "number", "description": "Amount in original currency"},
                    "currency": {"type": "string", "description": "Currency code: USD, ARS, EUR, etc."},
                    "category": {"type": "string", "description": "Category: Food, Transport, Salary, etc."},
                },
                "required": ["name", "amount", "currency", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Show total balance in USD.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report",
            "description": "Financial report by category for a period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string", "description": "day, week, month, year, or all (default: month)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transactions",
            "description": "List recent transactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many to show (default 10)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transaction",
            "description": "Delete a transaction by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Transaction ID"},
                },
                "required": ["id"],
            },
        },
    },
]

# Exchange rates cache (loaded once per session)
_rates = None

_FALLBACK_RATES = {
    "USD": 1.0, "EUR": 0.92, "GBP": 0.78, "ARS": 1200.0,
    "RUB": 91.5, "BRL": 5.17, "MXN": 17.5,
}


def _get_rates() -> dict:
    global _rates
    if _rates is not None:
        return _rates
    try:
        import requests
        resp = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        if resp.status_code == 200:
            _rates = resp.json().get("rates", {})
            _rates["USD"] = 1.0
            return _rates
    except Exception:
        pass
    _rates = dict(_FALLBACK_RATES)
    return _rates


def _to_usd(amount: float, currency: str) -> float:
    """Convert amount in any currency to USD."""
    rates = _get_rates()
    rate = rates.get(currency.upper(), 1.0)
    if rate == 0:
        return amount
    return amount / rate


def _ensure_table():
    import db
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            amount_usd REAL NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def execute(name: str, args: dict) -> str:
    import db
    from datetime import datetime, timedelta
    _ensure_table()
    conn = db._get_conn()

    if name == "add_transaction":
        amount = float(args["amount"])
        currency = args.get("currency", "USD").upper()
        usd = _to_usd(amount, currency)
        conn.execute(
            "INSERT INTO transactions (name, amount, currency, amount_usd, category, created_at) VALUES (?,?,?,?,?,?)",
            (args["name"], amount, currency, round(usd, 2), args["category"], datetime.now().isoformat())
        )
        conn.commit()
        return f"✓ {args['category']}: {amount:,.0f} {currency} (${usd:,.2f} USD) — {args['name']}"

    elif name == "get_balance":
        row = conn.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM transactions").fetchone()
        total = row[0]
        inc = conn.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM transactions WHERE category IN ('Salary','Income','Freelance','Investments')").fetchone()[0]
        exp = total - inc
        return f"💰 Income: ${inc:,.2f}\n💸 Expenses: ${exp:,.2f}\n{'─'*25}\n💵 Balance: ${inc - exp:,.2f}"

    elif name == "get_report":
        period = args.get("period", "month")
        now = datetime.now()
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0)
        elif period == "week":
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0)
        elif period == "year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
        else:
            start = datetime(1970, 1, 1)

        rows = conn.execute(
            "SELECT category, SUM(amount_usd) FROM transactions WHERE created_at >= ? GROUP BY category ORDER BY SUM(amount_usd) DESC",
            (start.isoformat(),)
        ).fetchall()

        if not rows:
            return f"📊 No transactions for period: {period}"

        total = sum(r[1] for r in rows)
        lines = [f"📊 Report ({period})", f"📅 {start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}", ""]
        for cat, amt in rows:
            pct = (amt / total * 100) if total else 0
            lines.append(f"  • {cat}: ${amt:,.2f} ({pct:.0f}%)")
        lines.append(f"\n💵 Total: ${total:,.2f}")
        return "\n".join(lines)

    elif name == "list_transactions":
        limit = args.get("limit", 10)
        rows = conn.execute(
            "SELECT id, name, amount, currency, amount_usd, category, created_at FROM transactions ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        if not rows:
            return "No transactions yet."
        lines = []
        for tid, tname, amt, cur, usd, cat, ts in rows:
            date = ts[:10]
            lines.append(f"#{tid} [{date}] {cat}: {amt:,.0f} {cur} (${usd:,.2f}) — {tname}")
        return "\n".join(lines)

    elif name == "delete_transaction":
        r = conn.execute("DELETE FROM transactions WHERE id=?", (args["id"],))
        conn.commit()
        if r.rowcount:
            return f"✓ Transaction #{args['id']} deleted"
        return f"Transaction #{args['id']} not found."

    return f"Unknown tool: {name}"
