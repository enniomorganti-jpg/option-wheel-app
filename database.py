# database.py
import json
import os
import sqlite3
import time
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import firebase_admin
from firebase_admin import credentials, firestore as fb_admin_fs

from google.api_core import exceptions as gexc

import config

# Tipi Firestore
firestore = fb_admin_fs  # alias coerente
FSDocRef = fb_admin_fs.DocumentReference
FSColRef = fb_admin_fs.CollectionReference
FSClient = fb_admin_fs.Client

_FIRESTORE_CLIENT: Optional[FSClient] = None
_CURRENT_USER_UID: Optional[str] = None

_COLLECTION_MAPPING = {
    config.ORDERS_CSV: "orders",
    config.POSITIONS_CSV: "positions",
    config.REALIZED_CSV: "realized",
    config.PRICES_CSV: "prices",
}

_DOC_ID_FIELDS = {
    "orders": "ID",
    "positions": "Underlying",
    "prices": "Underlying",
}

_SETTINGS_COLLECTION = "settings"
_SETTINGS_DOCUMENT = "config"
_DATE_FIELDS = ("OpenDate", "Expiry", "EventDate", "AsOf", "CloseDate")

# -------------------------
# Utility date
# -------------------------
def _norm_dates(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    for c in _DATE_FIELDS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    return df

# -------------------------
# Credenziali Firestore
# -------------------------
def _service_account_credentials() -> credentials.Certificate:
    # Preferisci un blocco dedicato ai service account
    if "firebase_service_account" in st.secrets:
        raw = dict(st.secrets["firebase_service_account"])
    elif "firebase" in st.secrets:
        # fallback: alcuni progetti mettono il SA dentro [firebase]
        fb = st.secrets["firebase"]
        raw = {
            "type": fb["type"],
            "project_id": fb["project_id"],
            "private_key_id": fb["private_key_id"],
            "private_key": fb["private_key"].replace("\\n", "\n"),
            "client_email": fb["client_email"],
            "client_id": fb["client_id"],
            "auth_uri": fb["auth_uri"],
            "token_uri": fb["token_uri"],
            "auth_provider_x509_cert_url": fb["auth_provider_x509_cert_url"],
            "client_x509_cert_url": fb["client_x509_cert_url"],
        }
    else:
        raise RuntimeError("Firebase service account non configurato nelle Streamlit secrets.")
    return credentials.Certificate(raw)

def _get_firestore_client() -> FSClient:
    global _FIRESTORE_CLIENT
    if _FIRESTORE_CLIENT is not None:
        return _FIRESTORE_CLIENT
    cred = _service_account_credentials()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    _FIRESTORE_CLIENT = fb_admin_fs.client()
    return _FIRESTORE_CLIENT

# -------------------------
# Utente corrente
# -------------------------
def set_current_user(uid: Optional[str]):
    """Registra l'utente corrente per le operazioni Firestore."""
    global _CURRENT_USER_UID
    _CURRENT_USER_UID = uid

def _require_user():
    if not _CURRENT_USER_UID:
        raise RuntimeError("Current user non impostato. Chiama set_current_user() dopo il login.")

def _user_document() -> FSDocRef:
    _require_user()
    client = _get_firestore_client()
    return client.collection("users").document(_CURRENT_USER_UID)

def _collection_for_name(name: str) -> Optional[str]:
    return _COLLECTION_MAPPING.get(name)

# -------------------------
# Serializzazione
# -------------------------
def _serialize_value(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except Exception:
            return value
    return value

def _serialize_dataframe(df: pd.DataFrame) -> List[Dict]:
    if df is None or df.empty:
        return []
    df_copy = df.copy()
    for field in _DATE_FIELDS:
        if field in df_copy.columns:
            df_copy[field] = pd.to_datetime(df_copy[field], errors="coerce").dt.strftime("%Y-%m-%d")
    records: List[Dict] = []
    for _, row in df_copy.iterrows():
        rec: Dict = {k: _serialize_value(v) for k, v in row.items()}
        records.append(rec)
    return records

# -------------------------
# Firestore helpers robusti
# -------------------------
def _fs_collection(name: str) -> Tuple[Optional[str], Optional[FSColRef]]:
    coll_name = _collection_for_name(name)
    if not coll_name:
        return None, None
    return coll_name, _user_document().collection(coll_name)

def _retry_once(fn, *args, **kwargs):
    """Piccolo retry con backoff: tenta, poi retry 1 volta dopo 0.5s."""
    try:
        return fn(*args, **kwargs)
    except (gexc.GoogleAPICallError, gexc.RetryError, gexc.DeadlineExceeded):
        time.sleep(0.5)
        return fn(*args, **kwargs)

@st.cache_data(ttl=30, show_spinner=False)
def _cached_fs_get(user_uid: str, coll_path: str, limit_n: int = 5000) -> List[Dict]:
    """Cache delle letture per ridurre carico e retry su Cloud."""
    client = _get_firestore_client()
    # ricostruisci ref
    coll = client.collection("users").document(user_uid).collection(coll_path)
    docs = _retry_once(coll.limit(limit_n).get)
    return [d.to_dict() for d in docs]

def _safe_warn(msg: str):
    try:
        st.warning(msg)
    except Exception:
        pass

# -------------------------
# CRUD Firestore
# -------------------------
def _delete_collection(collection_ref: FSColRef):
    # usa get() invece di stream() (più robusto su Cloud)
    docs = _retry_once(collection_ref.get)
    batch = _get_firestore_client().batch()
    count = 0
    for d in docs:
        batch.delete(d.reference)
        count += 1
        # commit a chunk per evitare batch enormi
        if count % 400 == 0:
            _retry_once(batch.commit)
            batch = _get_firestore_client().batch()
    _retry_once(batch.commit)

def _load_table_firestore(name: str, cols: List[str]) -> pd.DataFrame:
    coll_name, coll_ref = _fs_collection(name)
    if not coll_name or coll_ref is None:
        return pd.DataFrame(columns=cols)

    # se non c'è utente, torna vuoto (o potresti fare CSV)
    if not _CURRENT_USER_UID:
        return pd.DataFrame(columns=cols)

    try:
        rows = _cached_fs_get(_CURRENT_USER_UID, coll_name, limit_n=5000)
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)
    except (gexc.GoogleAPICallError, gexc.RetryError, gexc.DeadlineExceeded) as e:
        _safe_warn("Cloud DB lento o non disponibile: dati non caricati (mostro tabella vuota).")
        return pd.DataFrame(columns=cols)
    except Exception:
        # qualunque altro errore: fallback soft
        _safe_warn("Errore inatteso in lettura Cloud DB: tabella vuota.")
        return pd.DataFrame(columns=cols)

    # ordering stabile se possibile
    if not df.empty:
        if "ID" in df.columns:
            df = df.sort_values("ID")
        elif "Underlying" in df.columns:
            df = df.sort_values("Underlying")
        elif "EventDate" in df.columns:
            df = df.sort_values("EventDate")
        df = df.reset_index(drop=True)

    return _norm_dates(df)

def _doc_id_for_record(collection_name: str, record: Dict, index: int) -> str:
    key_field = _DOC_ID_FIELDS.get(collection_name)
    if key_field:
        value = record.get(key_field)
        if value not in (None, ""):
            return str(value)
    return str(index)

def _save_table_firestore(df: pd.DataFrame, name: str):
    coll_name, coll_ref = _fs_collection(name)
    if not coll_name or coll_ref is None:
        return
    user_doc = _user_document()
    _retry_once(user_doc.set, {}, merge=True)

    # cancella vecchi doc
    _delete_collection(coll_ref)

    records = _serialize_dataframe(df)
    client = _get_firestore_client()

    # scrivi in batch per performance/robustezza
    batch = client.batch()
    count = 0
    for idx, record in enumerate(records):
        doc_id = _doc_id_for_record(coll_name, record, idx)
        batch.set(coll_ref.document(doc_id), record)
        count += 1
        if count % 400 == 0:
            _retry_once(batch.commit)
            batch = client.batch()
    _retry_once(batch.commit)

def _load_settings_firestore() -> Dict:
    user_doc = _user_document()
    settings_doc = user_doc.collection(_SETTINGS_COLLECTION).document(_SETTINGS_DOCUMENT)
    try:
        snapshot = _retry_once(settings_doc.get)
        if not snapshot.exists:
            return {"starting_cash": config.DEFAULT_STARTING_CASH, **config.DEFAULT_IBKR_CONFIG}
        data = snapshot.to_dict() or {}
        return {"starting_cash": config.DEFAULT_STARTING_CASH, **config.DEFAULT_IBKR_CONFIG, **data}
    except (gexc.GoogleAPICallError, gexc.RetryError, gexc.DeadlineExceeded):
        _safe_warn("Impossibile leggere le impostazioni dal Cloud DB: uso default locali.")
        return {"starting_cash": config.DEFAULT_STARTING_CASH, **config.DEFAULT_IBKR_CONFIG}

def _save_settings_firestore(settings: Dict):
    user_doc = _user_document()
    _retry_once(user_doc.set, {}, merge=True)
    settings_doc = user_doc.collection(_SETTINGS_COLLECTION).document(_SETTINGS_DOCUMENT)
    _retry_once(settings_doc.set, settings)

def _wipe_all_data_firestore():
    user_doc = _user_document()
    for coll_name in set(_COLLECTION_MAPPING.values()):
        _delete_collection(user_doc.collection(coll_name))
    _delete_collection(user_doc.collection(_SETTINGS_COLLECTION))

# -------------------------
# CSV Backend
# -------------------------
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

# -------------------------
# SQLite Backend
# -------------------------
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

# -------------------------
# Public API
# -------------------------
def load_table(name: str, cols: List[str]) -> pd.DataFrame:
    # Se loggato → Firestore
    if _CURRENT_USER_UID:
        return _load_table_firestore(name, cols)

    # Altrimenti DB/CSV
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
    if _CURRENT_USER_UID:
        _save_table_firestore(df, name)
        return
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
    if _CURRENT_USER_UID:
        return _load_settings_firestore()
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
    if _CURRENT_USER_UID:
        _save_settings_firestore(settings)
        return
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
    if _CURRENT_USER_UID:
        _wipe_all_data_firestore()
        return
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
