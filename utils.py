# utils.py
import pandas as pd
import streamlit as st
from typing import List, Tuple
import config
from datetime import date

def clean_ticker(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    fixes = {"APPL": "AAPL"}
    return fixes.get(s, s)

def next_order_id(df: pd.DataFrame) -> int:
    return int(df["ID"].max() + 1) if ("ID" in df.columns and not df.empty) else 1

def positions_nonzero(df_positions: pd.DataFrame) -> pd.DataFrame:
    if df_positions is None or df_positions.empty:
        return pd.DataFrame(columns=["Underlying","Qty","AvgCost"])
    p = df_positions.copy()
    p["Qty"] = pd.to_numeric(p["Qty"], errors="coerce").fillna(0).astype(int)
    return p[p["Qty"] > 0].reset_index(drop=True)

def covered_contracts_available(positions_df: pd.DataFrame, underlying: str, orders_df: pd.DataFrame) -> int:
    ul = clean_ticker(underlying)
    
    # CORREZIONE: Verifica che positions_df abbia la colonna Underlying
    if positions_df.empty or "Underlying" not in positions_df.columns:
        return 0
        
    row = positions_df.loc[positions_df["Underlying"].astype(str).str.upper() == ul]
    if row.empty:
        return 0
        
    shares = int(row["Qty"].values[0])
    open_calls = 0
    
    if not orders_df.empty and "Underlying" in orders_df.columns and "Type" in orders_df.columns and "Status" in orders_df.columns:
        mask = ((orders_df["Underlying"].astype(str).str.upper() == ul) &
                (orders_df["Type"].str.upper() == "CALL") &
                (orders_df["Status"].str.upper() == "OPEN"))
        open_calls = int(orders_df.loc[mask, "Qty"].fillna(0).sum())
        
    return max(0, (shares // 100) - open_calls)

def format_currency(value, decimals=2):
    """
    Formatta un numero come valuta: 1.000.000,00 $
    """
    if value is None or pd.isna(value):
        return f"0,{''.join(['0']*decimals)} $"
    
    try:
        value = float(value)
        # Gestisce numeri negativi
        sign = "-" if value < 0 else ""
        abs_value = abs(value)
        
        # Formatta con separatori delle migliaia e decimali
        formatted = f"{abs_value:,.{decimals}f}"
        # Sostituisce , con . e . con , per il formato italiano
        formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{sign}{formatted} $"
    except (ValueError, TypeError):
        return f"0,{''.join(['0']*decimals)} $"

def format_date_it(dt):
    """Formatta data in DD/MM/YYYY"""
    if pd.isna(dt) or dt is None:
        return "—"
    try:
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return "—"

def calculate_dte(expiry_date):
    """Calcola Days To Expiration"""
    if pd.isna(expiry_date) or expiry_date is None:
        return "—"
    try:
        today = date.today()
        dte = (expiry_date - today).days
        return f"{max(0, dte)} gg"
    except Exception:
        return "—"