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

    # Track collateral commitments
    collateral_tracker = {}  # {underlying: {expiry_date: amount}}

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
        expiry_dt = r.get("Expiry")

        # OPEN EVENT - Sell PUT or CALL
        if pd.notna(open_dt) and side == "Sell" and qty > 0 and prem != 0:
            premium_cash = qty * 100.0 * prem - fees
            
            # For PUTs: subtract collateral immediately
            collateral_flow = 0.0
            if typ == "PUT":
                collateral_amount = strike * 100.0 * qty
                collateral_flow = -collateral_amount
                
                # Track this collateral for later release
                if ul not in collateral_tracker:
                    collateral_tracker[ul] = {}
                collateral_tracker[ul][expiry_dt] = collateral_tracker[ul].get(expiry_dt, 0) + collateral_amount

            total_cash_flow = premium_cash + collateral_flow
            
            ledger_rows.append({
                "Date": open_dt.date(), 
                "Underlying": ul, 
                "Event": f"{typ} Sold",
                "CashFlow": round(total_cash_flow, 2),
                "Note": f"Premium {format_currency(prem)} * 100 * {int(qty)} - fees {format_currency(fees)} | Collateral: {format_currency(collateral_flow) if collateral_flow != 0 else 'N/A'}",
                "Warning": ""
            })

        # CLOSE EVENTS
        if pd.notna(close_dt):
            if status == "Expired":
                if typ == "PUT":
                    # Release collateral when PUT expires
                    collateral_amount = collateral_tracker.get(ul, {}).get(expiry_dt, 0)
                    if collateral_amount > 0:
                        ledger_rows.append({
                            "Date": close_dt.date(), 
                            "Underlying": ul, 
                            "Event": "PUT Expired - Collateral Released",
                            "CashFlow": round(collateral_amount, 2),
                            "Note": f"Collateral released: {format_currency(collateral_amount)}",
                            "Warning": ""
                        })
                        # Remove from tracker
                        if ul in collateral_tracker and expiry_dt in collateral_tracker[ul]:
                            del collateral_tracker[ul][expiry_dt]
                else:  # CALL expired
                    ledger_rows.append({
                        "Date": close_dt.date(), 
                        "Underlying": ul, 
                        "Event": "CALL Expired",
                        "CashFlow": 0.0,
                        "Note": "Premium kept, no collateral involved",
                        "Warning": ""
                    })

            elif status == "Assigned" and typ == "PUT":
                # No cash flow change - collateral was already subtracted
                # Shares are acquired, but cash doesn't change
                delta_sh = int(qty * 100)
                _push_audit(close_dt, ul, "PUT Assigned (shares +)", delta_sh)
                
                ledger_rows.append({
                    "Date": close_dt.date(), 
                    "Underlying": ul, 
                    "Event": "PUT Assigned (shares acquired)",
                    "CashFlow": 0.0, 
                    "Note": f"Acquired {int(qty * 100)} shares @ {format_currency(strike)} - No cash flow (collateral already reserved)", 
                    "Warning": ""
                })

            elif status == "Calledaway" and typ == "CALL":
                # Add strike proceeds to cash balance
                strike_proceeds = strike * 100.0 * qty
                
                ledger_rows.append({
                    "Date": close_dt.date(), 
                    "Underlying": ul,
                    "Event": "CALL Called Away (shares sold)",
                    "CashFlow": round(strike_proceeds, 2),
                    "Note": f"Sold {int(qty * 100)} shares @ {format_currency(strike)}",
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