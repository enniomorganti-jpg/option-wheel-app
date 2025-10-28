# database.py
import json
import os
import sqlite3
import pandas as pd
from typing import Dict, List
import config

def _norm_dates(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    for c in ("OpenDate", "Expiry", "EventDate", "AsOf", "CloseDate"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    return df

# CSV Backend
def _load_csv(path: str, cols: List[str]) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        df = pd.DataFrame(columns=cols)
    return _norm_dates(df)

def _save_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False)

def _load_settings_json() -> Dict:
    try:
        with open(config.SETTINGS_JSON, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"starting_cash": config.DEFAULT_STARTING_CASH, **config.DEFAULT_IBKR_CONFIG}

def _save_settings_json(s: Dict):
    with open(config.SETTINGS_JSON, "w") as f:
        json.dump(s, f, indent=2)

# SQLite Backend  
def _db_conn():
    conn = sqlite3.connect(config.DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def _db_init():
    with _db_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders(
            ID INTEGER PRIMARY KEY,
            Underlying TEXT, Side TEXT, Type TEXT,
            OpenDate TEXT, Expiry TEXT, Strike REAL, Qty INTEGER,
            PricePerContract REAL, Fees REAL, Delta REAL,
            Notes TEXT, Status TEXT, CloseDate TEXT
        );
        CREATE TABLE IF NOT EXISTS positions(
            Underlying TEXT PRIMARY KEY, Qty INTEGER, AvgCost REAL
        );
        CREATE TABLE IF NOT EXISTS realized(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            Underlying TEXT, Event TEXT, EventDate TEXT,
            Shares INTEGER, Strike REAL, PremiumPerShare REAL,
            AvgCostAtEvent REAL, EquityPL REAL, TotalPL REAL, Notes TEXT
        );
        CREATE TABLE IF NOT EXISTS prices(
            Underlying TEXT PRIMARY KEY, Price REAL, AsOf TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(
            k TEXT PRIMARY KEY, v TEXT
        );
        """)
        conn.commit()

def _migrate_from_csv_if_needed():
    """Importa CSV se il DB è vuoto"""
    with _db_conn() as conn:
        def _has_rows(tbl):
            try:
                return conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone() is not None
            except Exception:
                return False
        
        mapping = {
            config.ORDERS_CSV: "orders",
            config.POSITIONS_CSV: "positions", 
            config.REALIZED_CSV: "realized",
            config.PRICES_CSV: "prices",
        }
        
        for fname, tbl in mapping.items():
            if not _has_rows(tbl) and os.path.isfile(fname):
                try:
                    df = pd.read_csv(fname)
                    df.to_sql(tbl, conn, if_exists="append", index=False)
                except Exception:
                    pass
        
        if not _has_rows("settings") and os.path.isfile(config.SETTINGS_JSON):
            try:
                with open(config.SETTINGS_JSON, "r") as f:
                    s = json.load(f)
                for k, v in s.items():
                    conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)", (k, json.dumps(v)))
                conn.commit()
            except Exception:
                pass

# Public API
def load_table(name: str, cols: List[str]) -> pd.DataFrame:
    if config.USE_DB:
        _db_init()
        _migrate_from_csv_if_needed()
        mapping = {
            config.ORDERS_CSV: "orders",
            config.POSITIONS_CSV: "positions",
            config.REALIZED_CSV: "realized", 
            config.PRICES_CSV: "prices",
        }
        tbl = mapping.get(name)
        if tbl:
            with _db_conn() as conn:
                try:
                    return pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
                except Exception:
                    return pd.DataFrame(columns=cols)
    return _load_csv(name, cols)

def save_table(df: pd.DataFrame, name: str):
    if config.USE_DB:
        _db_init()
        mapping = {
            config.ORDERS_CSV: "orders",
            config.POSITIONS_CSV: "positions",
            config.REALIZED_CSV: "realized",
            config.PRICES_CSV: "prices",
        }
        tbl = mapping.get(name)
        if tbl:
            df_copy = df.copy()
            for col in ("OpenDate","Expiry","EventDate","AsOf","CloseDate"):
                if col in df_copy.columns:
                    df_copy[col] = pd.to_datetime(df_copy[col], errors="coerce").dt.strftime("%Y-%m-%d")
            
            with _db_conn() as conn:
                conn.execute(f"DELETE FROM {tbl}")
                if not df_copy.empty:
                    df_copy.to_sql(tbl, conn, if_exists="append", index=False)
                conn.commit()
    else:
        _save_csv(df, name)

def load_settings() -> Dict:
    if config.USE_DB:
        _db_init()
        with _db_conn() as conn:
            rows = conn.execute("SELECT k, v FROM settings").fetchall()
        if not rows:
            return {"starting_cash": config.DEFAULT_STARTING_CASH, **config.DEFAULT_IBKR_CONFIG}
        return {r["k"]: json.loads(r["v"]) for r in rows}
    else:
        return _load_settings_json()

def save_settings(settings: Dict):
    if config.USE_DB:
        _db_init()
        with _db_conn() as conn:
            conn.execute("BEGIN")
            for k, v in settings.items():
                conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)", (k, json.dumps(v)))
            conn.commit()
    else:
        _save_settings_json(settings)

def wipe_all_data():
    """Cancella tutti i dati"""
    if config.USE_DB:
        with _db_conn() as conn:
            conn.executescript("""
                DELETE FROM orders; 
                DELETE FROM positions; 
                DELETE FROM realized; 
                DELETE FROM prices; 
                DELETE FROM settings;
            """)
            conn.commit()
    else:
        for path in [config.ORDERS_CSV, config.POSITIONS_CSV, config.REALIZED_CSV, 
                    config.PRICES_CSV, config.SETTINGS_JSON]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass