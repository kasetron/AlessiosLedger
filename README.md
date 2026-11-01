# Ledger

Personal finance tracker. Single-file Flask app with SQLite backend.

## Features

- Transaction log with categories and payment methods
- Monthly budget thresholds per category, with automatic rollover of unused budget to the following month
- Cumulative balances per payment method across all time
- Project budgets — fixed budgets for non-recurring expenses (e.g. travel costs) tracked across months
- Spending trend alerts mid-month
- Analysis tab: monthly spending, category comparison between two months and end-of-month balance, filterable by period, payment method and category (project spending kept separate)
- Full backup and restore via JSON export
- Automatic daily snapshot of the database in `backups/` (last 14 kept; to restore, stop the app and copy a snapshot over `ledger.db`)

## Stack

Python · Flask · SQLite · vanilla HTML/CSS/JS

## Run

```bash
pip install flask
python ledger.py
```

Opens automatically at `http://localhost:5000`.  
Data is stored in `ledger.db` in the same folder. Keep it backed up.

## Notes

`ledger.db` is excluded from version control. Use the in-app backup (↓ Backup button) to export data as JSON.  
To run a second isolated instance (e.g. for a different currency or context):

```bash
PORT=5001 python ledger.py
```
