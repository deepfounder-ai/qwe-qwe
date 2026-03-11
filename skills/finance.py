"""Finance tracking skill with live exchange rates from API."""

DESCRIPTION = "Finance tracking skill with tools: add_transaction, get_balance, get_report, list_transactions, delete_transaction"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_transaction",
            "description": "Adds a new transaction to SQLite DB with current timestamp",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Transaction name"},
                    "amount": {"type": "number", "description": "Transaction amount"},
                    "currency": {"type": "string", "description": "Currency code (USD, EUR, GBP, RUB)"},
                    "category": {"type": "string", "description": "Transaction category"}
                },
                "required": ["name", "amount", "currency", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Returns total USD balance (converts all currencies to USD)",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report",
            "description": "Returns formatted report of transactions in date range",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range": {"type": "string", "description": "Date range (default: last_30_days)"},
                    "category": {"type": "string", "description": "Optional category filter"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transactions",
            "description": "Lists all transactions with amount in original currency and USD equivalent",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transaction",
            "description": "Removes a transaction by ID",
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "integer", "description": "Transaction ID to delete"}
                },
                "required": ["transaction_id"],
            },
        },
    },
]

BASE_URL = "https://api.exchangerate-api.com/v4/latest/USD"


def fetch_exchange_rates():
    """Подгружает актуальные курсы валют из API."""
    try:
        import requests
        response = requests.get(BASE_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            rates = data.get("rates", {})
            # Keep all rates from API
            result = {"USD": 1.0}
            for code in ["EUR", "GBP", "RUB", "ARS", "BRL", "MXN", "CLP", "COP"]:
                if code in rates:
                    result[code] = rates[code]
            return result
        else:
            print(f"API error: {response.status_code}")
            return {"USD": 1.0, "EUR": 0.92, "GBP": 0.78, "RUB": 91.5}
    except Exception as e:
        print(f"Failed to fetch rates: {e}")
        return {"USD": 1.0, "EUR": 0.92, "GBP": 0.78, "RUB": 91.5}


EXCHANGE_RATES = fetch_exchange_rates()

print(f"Loaded exchange rates: {EXCHANGE_RATES}")


def _ensure_table():
    import db
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            category TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def execute(name: str, args: dict) -> str:
    _ensure_table()
    if name == "add_transaction":
        try:
            import db
            cursor = db._get_conn().cursor()
            from datetime import datetime
            timestamp = datetime.now().isoformat()
            cursor.execute(
                "INSERT INTO transactions (name, amount, currency, category, created_at) VALUES (?, ?, ?, ?, ?)",
                (args.get("name"), args.get("amount"), args.get("currency"), args.get("category"), timestamp)
            )
            db._get_conn().commit()
            rate = EXCHANGE_RATES.get(args.get("currency", "USD"), 1.0)
            return f"Transaction added: {args.get('name')} ({args.get('currency')}) - ${float(args.get('amount')) / rate} USD"
        except Exception as e:
            return f"Error adding transaction: {str(e)}"

    elif name == "get_balance":
        try:
            import db
            cursor = db._get_conn().cursor()
            cursor.execute("SELECT amount, currency FROM transactions")
            rows = cursor.fetchall()
            total_usd = 0.0
            for amount, currency in rows:
                rate = EXCHANGE_RATES.get(currency, 1.0)
                total_usd += float(amount) / rate
            return f"Total Balance: ${total_usd:.2f} USD"
        except Exception as e:
            return f"Error getting balance: {str(e)}"

    elif name == "get_report":
        try:
            import db
            cursor = db._get_conn().cursor()
            date_range = args.get("date_range", "last_30_days")

            if date_range == "last_30_days":
                cursor.execute("""
                    SELECT name, amount, currency, category, created_at 
                    FROM transactions 
                    WHERE strftime('%Y-%m-%d', created_at) >= datetime('now', '-30 days')
                """)
            else:
                cursor.execute("""
                    SELECT name, amount, currency, category, created_at 
                    FROM transactions 
                    WHERE strftime('%Y-%m-%d', created_at) >= ?
                """, (date_range,))

            rows = cursor.fetchall()
            if not rows:
                return "No transactions found in the specified range."

            lines = ["=== Transaction Report ==="]
            for row in rows:
                name, amount, currency, category, created_at = row
                usd_amount = float(amount) / EXCHANGE_RATES.get(currency, 1.0)
                lines.append(f"{name} | {amount} {currency} | ${usd_amount:.2f} USD")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting report: {str(e)}"

    elif name == "list_transactions":
        try:
            import db
            cursor = db._get_conn().cursor()
            cursor.execute("SELECT id, name, amount, currency, category, created_at FROM transactions")
            rows = cursor.fetchall()

            if not rows:
                return "No transactions found."

            lines = ["=== All Transactions ==="]
            for row in rows:
                tid, name, amount, currency, category, created_at = row
                usd_amount = float(amount) / EXCHANGE_RATES.get(currency, 1.0)
                lines.append(f"ID:{tid} | {name} | {amount} {currency} | ${usd_amount:.2f} USD")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing transactions: {str(e)}"

    elif name == "delete_transaction":
        try:
            import db
            cursor = db._get_conn().cursor()
            transaction_id = args.get("transaction_id")
            cursor.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,))
            if not cursor.fetchone():
                return f"Transaction with ID {transaction_id} not found."
            cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            db._get_conn().commit()
            return f"Transaction ID {transaction_id} deleted successfully."
        except Exception as e:
            return f"Error deleting transaction: {str(e)}"

    return f"Unknown tool: {name}"
