# config.py
import os
from pathlib import Path

# File paths
ORDERS_CSV = "orders.csv"
POSITIONS_CSV = "positions.csv"
REALIZED_CSV = "realized.csv"
PRICES_CSV = "prices.csv"
SETTINGS_JSON = "settings.json"
DB_PATH = "optionwheel.db"

# App settings
USE_DB = False  # True per SQLite, False per CSV
DEFAULT_STARTING_CASH = 1_000_000.0
DEFAULT_IBKR_CONFIG = {
    "host": "127.0.0.1",
    "port": 7497,
    "client_id": 1
}

# UI Settings - ORDINE DINAMICO DELLE SEZIONI
SECTION_ORDER = [
    "overview",
    "market_prices", 
    "portfolio_composition",
    "option_expiries",
    "covered_call_coverage",
    "moneyness",
    "delta_distribution",
    "realized_pl"
]

# Formatting hints
MONEY_HINTS = {
    "price", "last", "avgcost", "marketvalue", "value", "amount", "notional",
    "totalpl", "equitypl", "premium", "premiumpershare", "fees", "valore", "cash", "strike"
}

PERC_HINTS = {"perc", "%", "diff%", "delta%"}