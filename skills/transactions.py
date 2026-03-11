"""Transactions skill for managing financial records."""

import sqlite3
from datetime import datetime
import json
from pathlib import Path


DB_PATH = Path("/home/kirco/qwe-qwe/qwe_qwe.db")
EXCHANGE_RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.78,
    "RUB": 91.5,
    "ARS": 1000.0  # Примерный курс аргентинского песо
}


def _get_cursor():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return cursor


def execute(name: str, args: dict) -> str:
    if name == "add_transaction":
        try:
            cursor = _get_cursor()
            timestamp = datetime.now().isoformat()
            cursor.execute(
                "INSERT INTO transactions (name, amount, currency, category, created_at) VALUES (?, ?, ?, ?, ?)",
                (args.get("name"), args.get("amount"), args.get("currency"), args.get("category"), timestamp)
            )
            conn = sqlite3.connect(DB_PATH)
            conn.commit()
            conn.close()
            
            rate = EXCHANGE_RATES.get(args.get("currency", "USD"), 1.0)
            return f"Транзакция добавлена: {args.get('name')} ({args.get('currency')}) - ${float(args.get('amount')) * rate} USD"
        except Exception as e:
            return f"Ошибка при добавлении транзакции: {str(e)}"

    elif name == "get_balance":
        try:
            cursor = _get_cursor()
            cursor.execute("SELECT amount, currency FROM transactions")
            rows = cursor.fetchall()
            total_usd = 0.0
            for amount, currency in rows:
                rate = EXCHANGE_RATES.get(currency, 1.0)
                total_usd += float(amount) * rate
            return f"Общий баланс: ${total_usd:.2f} USD"
        except Exception as e:
            return f"Ошибка при получении баланса: {str(e)}"

    elif name == "get_report":
        try:
            cursor = _get_cursor()
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
                return "В указанном диапазоне транзакций не найдено."

            lines = ["=== Отчёт по транзакциям ==="]
            for row in rows:
                name, amount, currency, category, created_at = row
                usd_amount = float(amount) * EXCHANGE_RATES.get(currency, 1.0)
                lines.append(f"{name} | {amount} {currency} | ${usd_amount:.2f} USD")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка при получении отчёта: {str(e)}"

    elif name == "list_transactions":
        try:
            cursor = _get_cursor()
            cursor.execute("SELECT id, name, amount, currency, category, created_at FROM transactions")
            rows = cursor.fetchall()

            if not rows:
                return "Транзакций не найдено."

            lines = ["=== Все транзакции ==="]
            for row in rows:
                tid, name, amount, currency, category, created_at = row
                usd_amount = float(amount) * EXCHANGE_RATES.get(currency, 1.0)
                lines.append(f"ID:{tid} | {name} | {amount} {currency} | ${usd_amount:.2f} USD")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка при перечислении транзакций: {str(e)}"

    elif name == "delete_transaction":
        try:
            cursor = _get_cursor()
            transaction_id = args.get("transaction_id")
            cursor.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,))
            if not cursor.fetchone():
                return f"Транзакция с ID {transaction_id} не найдена."
            cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            conn = sqlite3.connect(DB_PATH)
            conn.commit()
            conn.close()
            return f"Транзакция ID {transaction_id} успешно удалена."
        except Exception as e:
            return f"Ошибка при удалении транзакции: {str(e)}"

    return f"Неизвестный инструмент: {name}"


def fetch_exchange_rates():
    """Подгружает актуальные курсы валют из API."""
    try:
        import requests
        BASE_URL = "https://api.exchangerate-api.com/v4/latest/USD"
        response = requests.get(BASE_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            rates = data.get("rates", {})
            return {
                "USD": 1.0,
                "EUR": rates.get("EUR", 0.92),
                "GBP": rates.get("GBP", 0.78),
                "RUB": rates.get("RUB", 91.5),
                "ARS": rates.get("ARS", 1000.0)
            }
        else:
            print(f"API error: {response.status_code}")
            return {"USD": 1.0, "EUR": 0.92, "GBP": 0.78, "RUB": 91.5, "ARS": 1000.0}
    except Exception as e:
        print(f"Failed to fetch rates: {e}")
        return {"USD": 1.0, "EUR": 0.92, "GBP": 0.78, "RUB": 91.5, "ARS": 1000.0}
