# Task: Create a "finance" skill for qwe-qwe

Create file `skills/finance.py` — a personal finance tracker skill.

## Skill Format

The file must follow this exact structure:
```python
"""Finance skill — personal income/expense tracker."""

DESCRIPTION = "Track income, expenses, balance, and generate reports"

TOOLS = [
    # list of tool definitions
]

def execute(name: str, args: dict) -> str:
    # handle each tool
```

## Required Tools

### 1. `add_transaction`
- params: `type` (income/expense), `amount` (float), `currency` (str, default "USD"), `category` (str), `description` (str, optional)
- Auto-convert all currencies to USD using fallback rates (no API calls — offline!):
  ```python
  RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "ARS": 0.001, "RUB": 0.011, "BRL": 0.20}
  ```
- Store in SQLite table `transactions`: id, type, amount_original, currency, amount_usd, category, description, created_at
- Return confirmation with USD amount

### 2. `get_balance`
- No params
- Return: total income, total expenses, balance (all in USD)
- Format nicely with emoji

### 3. `get_report`
- params: `period` (day/week/month/year/all, default "month")
- Group by category, show totals
- Include income breakdown, expense breakdown, and net balance
- Format with emoji: 💰 income, 💸 expense, 📊 header

### 4. `list_transactions`
- params: `limit` (int, default 10)
- Show recent transactions with date, category, amount, description

### 5. `delete_transaction`
- params: `id` (int)
- Delete by ID, return confirmation

## Implementation Rules

1. Use `import db` and `db._get_conn()` to get the shared SQLite connection
2. Create table with `_ensure_table()` function (called in execute before any query)
3. Default categories — auto-created in table:
   - Income: Salary, Freelance, Investments, Other Income
   - Expense: Food, Transport, Housing, Utilities, Entertainment, Health, Shopping, Other
4. Dates use `datetime.now()` — import from datetime
5. Currency conversion uses hardcoded RATES dict (fully offline!)
6. All money formatting: `${amount:,.2f}`
7. Report periods calculate start_date from now:
   - day: today midnight
   - week: this Monday
   - month: 1st of month
   - year: Jan 1st
   - all: epoch
8. Keep it under 200 lines

## Example output

```
✓ Added: 💸 Food — $15.00 (15 USD) "Lunch"
```

```
📊 Report (Month)
2026-03-01 → 2026-03-11

💰 INCOME: $1,000.00
  • Salary: $1,000.00

💸 EXPENSES: $250.00
  • Food: $120.00
  • Transport: $80.00
  • Entertainment: $50.00

💵 Balance: $750.00
```
