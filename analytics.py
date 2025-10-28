# analytics.py
import pandas as pd
import numpy as np
from datetime import date
from typing import Tuple
from utils import clean_ticker, format_currency

def _num(x, dflt=0.0):
    try:
        return float(x)
    except Exception:
        return dflt

def _days_open(row) -> int:
    od = pd.to_datetime(row.get("OpenDate"), errors="coerce")
    cd = pd.to_datetime(row.get("CloseDate"), errors="coerce")
    end = cd if pd.notna(cd) else pd.to_datetime(date.today())
    if pd.isna(od):
        return 1
    d = (end.date() - od.date()).days
    return max(int(d), 1)

def compute_order_metrics(row: pd.Series) -> dict:
    qty = _num(row.get("Qty", 0))
    prem = _num(row.get("PricePerContract", 0))
    strike = _num(row.get("Strike", 0))
    delta = _num(row.get("Delta", 0))
    
    income = qty * 100.0 * prem
    capital = (strike * 100.0 * qty) if strike and qty else 0.0
    roi = (income / capital) if capital > 0 else None
    days = _days_open(row)
    ann_roi = (roi * (365.0 / days)) if (roi is not None and days > 0) else None
    prob_itm = abs(delta)
    
    return {
        "income": income, 
        "roi": roi, 
        "ann_roi": ann_roi, 
        "prob_itm": prob_itm, 
        "days_open": days
    }

def compute_calledaway_pl_row(assigned_avg_cost, strike_call, premium_call, shares, fees=0.0):
    equity_pl = (strike_call - assigned_avg_cost) * shares
    option_pl = premium_call * shares
    total_pl = equity_pl + option_pl - fees
    return round(equity_pl, 2), round(total_pl, 2)

def rebuild_realized_from_orders(orders_df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "Underlying", "Event", "EventDate", "Shares", "Strike", "PremiumPerShare",
        "AvgCostAtEvent", "EquityPL", "TotalPL", "Notes"
    ]
    
    if orders_df is None or orders_df.empty:
        return pd.DataFrame(columns=cols)

    o = orders_df.copy()
    for col in ["OpenDate", "CloseDate", "Expiry"]:
        if col in o.columns:
            o[col] = pd.to_datetime(o[col], errors="coerce")
    
    o["Strike"] = pd.to_numeric(o["Strike"], errors="coerce")
    o["Qty"] = pd.to_numeric(o["Qty"], errors="coerce").fillna(0).astype(int)
    o["PricePerContract"] = pd.to_numeric(o["PricePerContract"], errors="coerce").fillna(0.0)
    o["Fees"] = pd.to_numeric(o["Fees"], errors="coerce").fillna(0.0)

    inventory = {}
    rows = []
    o = o.sort_values(["OpenDate", "ID"], ascending=[True, True])

    for _, r in o.iterrows():
        ul = clean_ticker(r["Underlying"])
        typ = str(r["Type"]).upper()
        status = str(r["Status"]).upper()
        qty = int(r["Qty"])
        shares = qty * 100
        strike = _num(r["Strike"])
        premium = _num(r["PricePerContract"])
        fees = _num(r["Fees"])
        open_date = r["OpenDate"].date() if pd.notna(r["OpenDate"]) else None
        close_date = r["CloseDate"].date() if pd.notna(r["CloseDate"]) else None

        inventory.setdefault(ul, {"shares": 0, "avg": 0.0})

        if typ == "PUT" and status == "ASSIGNED":
            avg_in = strike - premium
            curr = inventory[ul]
            total_shares = curr["shares"] + shares
            
            if total_shares <= 0:
                curr["shares"], curr["avg"] = 0, 0.0
            else:
                curr["avg"] = (curr["shares"] * curr["avg"] + shares * avg_in) / total_shares
                curr["shares"] = total_shares

            rows.append({
                "Underlying": ul, "Event": "Assigned", "EventDate": open_date or close_date,
                "Shares": shares, "Strike": strike, "PremiumPerShare": premium,
                "AvgCostAtEvent": round(curr["avg"], 4), "EquityPL": 0.0,
                "TotalPL": round(premium * shares - fees, 2), "Notes": ""
            })

        elif typ == "CALL" and status == "CALLEDAWAY":
            curr = inventory[ul]
            used_shares = min(curr["shares"], shares) if curr["shares"] > 0 else shares
            assigned_avg = curr["avg"] if curr["avg"] > 0 else strike
            equity_pl, total_pl = compute_calledaway_pl_row(
                assigned_avg, strike, premium, used_shares, fees
            )
            
            curr["shares"] = max(0, curr["shares"] - used_shares)
            if curr["shares"] == 0:
                curr["avg"] = 0.0

            rows.append({
                "Underlying": ul, "Event": "CalledAway", "EventDate": close_date or open_date,
                "Shares": used_shares, "Strike": strike, "PremiumPerShare": premium,
                "AvgCostAtEvent": round(assigned_avg, 4), "EquityPL": equity_pl,
                "TotalPL": total_pl, "Notes": ""
            })

    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["EventDate"] = pd.to_datetime(df["EventDate"], errors="coerce").dt.date
    return df

def build_cash_ledger_inventory_aware(orders_df: pd.DataFrame, positions_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    ledger_rows, audit_rows = [], []
    
    if orders_df.empty:
        return (
            pd.DataFrame(columns=["Date", "Underlying", "Event", "CashFlow", "Note", "Warning"]),
            pd.DataFrame(columns=["Date", "Underlying", "Event", "SharesDelta"])
        )

    o = orders_df.copy()
    for col in ("OpenDate", "CloseDate", "Expiry"):
        if col in o.columns:
            o[col] = pd.to_datetime(o[col], errors="coerce")
    
    if "ID" not in o.columns:
        o["ID"] = np.arange(1, len(o) + 1)
    
    o = o.sort_values(["OpenDate", "ID"]).reset_index(drop=True)

    # Initialize balance from current positions (solo per logica assegnazione CALL)
    balance = {}
    if not positions_df.empty:
        for _, r in positions_df.iterrows():
            ul = clean_ticker(r.get("Underlying", ""))
            qty = int(r.get("Qty", 0) or 0)
            balance[ul] = balance.get(ul, 0) + qty

    def _push_audit(ts, ul, ev, delta):
        audit_rows.append({
            "Date": ts.date() if pd.notna(ts) else None,
            "Underlying": ul,
            "Event": ev,
            "SharesDelta": delta
        })

    for _, r in o.iterrows():
        ul = clean_ticker(r.get("Underlying", ""))
        typ = str(r.get("Type", "")).upper()
        side = str(r.get("Side", "Sell")).title()
        qty = _num(r.get("Qty", 0))
        prem = _num(r.get("PricePerContract", 0))
        fees = _num(r.get("Fees", 0))
        strike = _num(r.get("Strike", 0))
        status = str(r.get("Status", "")).title()
        open_dt = r.get("OpenDate")
        close_dt = r.get("CloseDate")

        # Nella sezione OPEN - riga ~190
        if pd.notna(open_dt) and side == "Sell" and qty > 0 and prem != 0:
            # CORREGGI: aggiungi * 100
            premium_cash = qty * 100.0 * prem - fees
            
            collateral_note = f" | Collateral reserved: {format_currency(strike)} * 100 * {int(qty)}" if typ == "PUT" else ""
            
            ledger_rows.append({
                "Date": open_dt.date(), 
                "Underlying": ul, 
                "Event": f"{typ} Sold",
                "CashFlow": round(premium_cash, 2),
                "Note": f"Premium {format_currency(prem)} * 100 * {int(qty)} - fees {format_currency(fees)}{collateral_note}",
                "Warning": ""
            })

        # CLOSE: Various events
        if pd.notna(close_dt):
            if status == "Assigned" and typ == "PUT":
                delta_sh = int(qty * 100)
                balance[ul] = balance.get(ul, 0) + delta_sh
                _push_audit(close_dt, ul, "PUT Assigned (shares +)", delta_sh)
                
                # Cash flow NEGATIVO per acquisto shares (collateral non era stato sottratto prima)
                cash_flow = -strike * 100.0 * qty
                
                ledger_rows.append({
                    "Date": close_dt.date(), 
                    "Underlying": ul, 
                    "Event": "PUT Assigned (buy shares)",
                    "CashFlow": round(cash_flow, 2), 
                    "Note": f"Buy {int(qty * 100)} @ {format_currency(strike)} using collateral", 
                    "Warning": ""
                })

            elif status == "Calledaway" and typ == "CALL":
                needed = int(qty * 100)
                have = balance.get(ul, 0)
                
                if have >= needed:
                    balance[ul] = have - needed
                    _push_audit(close_dt, ul, "CALL Called Away (shares -)", -needed)
                    
                    cash = strike * 100.0 * qty
                    ledger_rows.append({
                        "Date": close_dt.date(), 
                        "Underlying": ul,
                        "Event": "CALL Called Away (sell shares)",
                        "CashFlow": round(cash, 2),
                        "Note": f"Sell {needed} @ {format_currency(strike)}",
                        "Warning": ""
                    })
                else:
                    _push_audit(close_dt, ul, "CALL Called Away (not enough shares)", 0)
                    ledger_rows.append({
                        "Date": close_dt.date(), 
                        "Underlying": ul,
                        "Event": "CALL Called Away (sell shares)",
                        "CashFlow": 0.0,
                        "Note": f"WARNING: not enough shares (need {needed}, have {have}) → cashflow skipped",
                        "Warning": "NOT_ENOUGH_SHARES"
                    })

            elif status == "Expired":
                _push_audit(close_dt, ul, f"{typ} Expired", 0)
                
                # PER PUT: NESSUN cash flow per collateral (non era stato sottratto)
                expired_note = "Premium kept | Collateral released" if typ == "PUT" else "No extra cash; premium kept"
                
                ledger_rows.append({
                    "Date": close_dt.date(), 
                    "Underlying": ul, 
                    "Event": f"{typ} Expired",
                    "CashFlow": 0.0, 
                    "Note": expired_note, 
                    "Warning": ""
                })

    # Gestione dei DataFrame vuoti
    ledger = pd.DataFrame(ledger_rows)
    if not ledger.empty:
        ledger = ledger.sort_values(["Date", "Underlying", "Event"]).reset_index(drop=True)
    else:
        ledger = pd.DataFrame(columns=["Date", "Underlying", "Event", "CashFlow", "Note", "Warning"])
    
    audit = pd.DataFrame(audit_rows)
    if not audit.empty:
        audit = audit.sort_values(["Date", "Underlying"]).reset_index(drop=True)
    else:
        audit = pd.DataFrame(columns=["Date", "Underlying", "Event", "SharesDelta"])
    
    return ledger, audit

def cash_timeline_df(cash_ledger: pd.DataFrame, starting_cash: float) -> pd.DataFrame:
    if cash_ledger is None or cash_ledger.empty:
        return pd.DataFrame(columns=["Date", "DailyFlow", "CumulativeCash"])
    
    agg = (cash_ledger.groupby("Date", as_index=False)["CashFlow"].sum()
           .rename(columns={"CashFlow": "DailyFlow"}))
    agg = agg.sort_values("Date").reset_index(drop=True)
    agg["CumulativeCash"] = starting_cash + agg["DailyFlow"].cumsum()
    
    return agg