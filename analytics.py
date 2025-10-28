# analytics.py
import pandas as pd
import numpy as np
from datetime import date
from utils import clean_ticker  # format_currency non serve qui

# -----------------------------
# Helpers generali
# -----------------------------
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
    qty    = _num(row.get("Qty", 0))
    prem   = _num(row.get("PricePerContract", 0))
    strike = _num(row.get("Strike", 0))
    delta  = _num(row.get("Delta", 0))

    income  = qty * 100.0 * prem
    capital = (strike * 100.0 * qty) if strike and qty else 0.0
    roi     = (income / capital) if capital > 0 else None
    days    = _days_open(row)
    ann_roi = (roi * (365.0 / days)) if (roi is not None and days > 0) else None
    prob_itm = abs(delta)

    return {
        "income": income,
        "roi": roi,
        "ann_roi": ann_roi,
        "prob_itm": prob_itm,
        "days_open": days
    }

# -----------------------------
# Realized (equity-only)
# -----------------------------
def compute_calledaway_pl_row(assigned_avg_cost, strike_call, premium_call, shares, fees=0.0):
    """
    P/L quando una CALL viene chiamata (Called Away).
    - Equity P/L = (strike_call - assigned_avg_cost) * shares
    - Option P/L = premium_call * shares
    - Total P/L  = Equity P/L + Option P/L - fees
    """
    equity_pl = (float(strike_call) - float(assigned_avg_cost)) * int(shares)
    option_pl = float(premium_call) * int(shares)
    total_pl  = equity_pl + option_pl - float(fees)
    return round(equity_pl, 2), round(total_pl, 2)

def rebuild_realized_from_orders(orders_df: pd.DataFrame) -> pd.DataFrame:
    """
    Ricostruisce realized come EQUITY-ONLY:
      - PUT Assigned: aggiorna inventario a costo medio = strike (NON netto premio).
                      realized=0 (il premio viene già nel cash ledger).
      - CALL CalledAway: realized = (strike_call - avg_cost) * shares - fees
      - Nessun premio conteggiato qui.
    """
    cols = [
        "Underlying", "Event", "EventDate", "Shares", "Strike", "PremiumPerShare",
        "AvgCostAtEvent", "EquityPL", "TotalPL", "Notes"
    ]
    if orders_df is None or orders_df.empty:
        return pd.DataFrame(columns=cols)

    o = orders_df.copy()

    # Normalizzazione
    for col in ("OpenDate", "CloseDate", "Expiry"):
        if col in o.columns:
            o[col] = pd.to_datetime(o[col], errors="coerce")

    o["Strike"] = pd.to_numeric(o.get("Strike"), errors="coerce")
    o["Qty"] = pd.to_numeric(o.get("Qty"), errors="coerce").fillna(0).astype(int)
    o["PricePerContract"] = pd.to_numeric(o.get("PricePerContract"), errors="coerce").fillna(0.0)
    o["Fees"] = pd.to_numeric(o.get("Fees"), errors="coerce").fillna(0.0)

    # ID per ordinamento stabile
    if "ID" not in o.columns:
        o["ID"] = np.arange(1, len(o) + 1)

    inventory = {}  # ul -> {"shares": int, "avg": float}
    rows = []

    o = o.sort_values(["OpenDate", "ID"], ascending=[True, True])

    for _, r in o.iterrows():
        ul     = clean_ticker(r.get("Underlying", ""))
        typ    = str(r.get("Type", "")).upper()
        status = str(r.get("Status", "")).upper()
        qty    = int(r.get("Qty", 0))
        shares = qty * 100
        strike = _num(r.get("Strike"))
        premium = _num(r.get("PricePerContract"))
        fees = _num(r.get("Fees"))
        open_date  = r["OpenDate"].date()  if pd.notna(r.get("OpenDate"))  else None
        close_date = r["CloseDate"].date() if pd.notna(r.get("CloseDate")) else None

        inventory.setdefault(ul, {"shares": 0, "avg": 0.0})

        if typ == "PUT" and status == "ASSIGNED":
            # Costo medio = STRIKE (non netto del premio)
            avg_in = strike
            cur = inventory[ul]
            tot_shares = cur["shares"] + shares
            if tot_shares <= 0:
                cur["shares"], cur["avg"] = 0, 0.0
            else:
                cur["avg"] = (cur["shares"] * cur["avg"] + shares * avg_in) / tot_shares
                cur["shares"] = tot_shares

            rows.append({
                "Underlying": ul,
                "Event": "Assigned",
                "EventDate": open_date or close_date,
                "Shares": shares,
                "Strike": strike,
                "PremiumPerShare": premium,
                "AvgCostAtEvent": round(cur["avg"], 4),
                "EquityPL": 0.0,
                "TotalPL": 0.0,       # premi nel cash ledger, qui niente
                "Notes": ""
            })

        elif typ == "CALL" and status == "CALLEDAWAY":
            cur = inventory[ul]
            used_shares = min(cur["shares"], shares) if cur["shares"] > 0 else shares
            assigned_avg = cur["avg"] if cur["avg"] > 0 else strike

            equity_pl, total_pl = compute_calledaway_pl_row(
                assigned_avg_cost=assigned_avg,
                strike_call=strike,
                premium_call=premium,
                shares=used_shares,
                fees=fees
            )

            cur["shares"] = max(0, cur["shares"] - used_shares)
            if cur["shares"] == 0:
                cur["avg"] = 0.0

            rows.append({
                "Underlying": ul,
                "Event": "CalledAway",
                "EventDate": close_date or open_date,
                "Shares": used_shares,
                "Strike": strike,
                "PremiumPerShare": premium,
                "AvgCostAtEvent": round(assigned_avg, 4),
                "EquityPL": equity_pl,
                "TotalPL": total_pl,
                "Notes": ""
            })

    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["EventDate"] = pd.to_datetime(df["EventDate"], errors="coerce").dt.date
    return df

# -----------------------------
# Cash ledger (premi & movimenti)
# -----------------------------
def build_cash_ledger_inventory_aware(orders_df: pd.DataFrame, positions_df: pd.DataFrame):
    """
    Ledger di cassa (inventory-aware):
      - OPEN SELL (PUT/CALL): + qty*100*premium - fees
      - PUT Assigned:         - strike*100*qty
      - CALL CalledAway:      + strike*100*qty
      - Expired:               0  (il premio è già incassato all'OPEN)

    Ritorna:
      ledger_df: ["Date","Underlying","Event","CashFlow","Note","Warning"]
      audit_df : ["Date","Underlying","Event","SharesDelta","BalanceAfter"]
    """
    ledger_cols = ["Date", "Underlying", "Event", "CashFlow", "Note", "Warning"]
    audit_cols  = ["Date", "Underlying", "Event", "SharesDelta", "BalanceAfter"]

    if orders_df is None or orders_df.empty:
        return (pd.DataFrame(columns=ledger_cols), pd.DataFrame(columns=audit_cols))

    # Copia e normalizza
    o = orders_df.copy()
    for c in ("OpenDate", "CloseDate", "Expiry"):
        if c in o.columns:
            o[c] = pd.to_datetime(o[c], errors="coerce")

    if "ID" not in o.columns:
        o["ID"] = np.arange(1, len(o) + 1)

    for c in ("Strike", "Qty", "PricePerContract", "Fees"):
        if c in o.columns:
            o[c] = pd.to_numeric(o[c], errors="coerce")

    o = o.sort_values(["OpenDate", "ID"]).reset_index(drop=True)

    # Inventario azioni "audit"
    bal = {}
    if positions_df is not None and not positions_df.empty:
        for _, rr in positions_df.iterrows():
            ulp = clean_ticker(rr.get("Underlying", ""))
            qtyp = int(pd.to_numeric(rr.get("Qty", 0), errors="coerce") or 0)
            bal[ulp] = bal.get(ulp, 0) + qtyp

    ledger_rows, audit_rows = [], []

    def push_audit(ts, ul, ev, delta):
        audit_rows.append({
            "Date": ts.date() if pd.notna(ts) else None,
            "Underlying": ul,
            "Event": ev,
            "SharesDelta": int(delta),
            "BalanceAfter": int(bal.get(ul, 0))
        })

    for _, r in o.iterrows():
        ul     = clean_ticker(r.get("Underlying", ""))
        typ    = str(r.get("Type", "") or "").upper()
        side   = str(r.get("Side", "Sell") or "").title()
        qty    = float(r.get("Qty", 0) or 0.0)
        prem   = float(r.get("PricePerContract", 0.0) or 0.0)
        fees   = float(r.get("Fees", 0.0) or 0.0)
        strike = float(r.get("Strike", 0.0) or 0.0)
        status = str(r.get("Status", "") or "").title()
        open_dt  = r.get("OpenDate")
        close_dt = r.get("CloseDate")

        # OPEN: incasso premio (solo vendite)
        if pd.notna(open_dt) and side == "Sell" and qty > 0 and prem != 0:
            cash = qty * 100.0 * prem - fees
            ledger_rows.append({
                "Date": open_dt.date(),
                "Underlying": ul,
                "Event": f"{typ} Sold",
                "CashFlow": round(cash, 2),
                "Note": f"Premium {prem:.4f} * 100 * {int(qty)} - fees {fees:.2f}",
                "Warning": ""
            })

        # CHIUSURA: effetti cassa
        if pd.notna(close_dt):
            if status == "Assigned" and typ == "PUT":
                # acquisto azioni
                delta_sh = int(qty * 100)
                bal[ul] = bal.get(ul, 0) + delta_sh
                push_audit(close_dt, ul, "PUT Assigned (shares +)", delta_sh)

                cash = - strike * 100.0 * qty
                ledger_rows.append({
                    "Date": close_dt.date(),
                    "Underlying": ul,
                    "Event": "PUT Assigned (buy shares)",
                    "CashFlow": round(cash, 2),
                    "Note": f"Buy {delta_sh} @ {strike:.2f}",
                    "Warning": ""
                })

            elif status == "Calledaway" and typ == "CALL":
                # vendita azioni
                need = int(qty * 100)
                have = int(bal.get(ul, 0))
                if have >= need:
                    bal[ul] = have - need
                    push_audit(close_dt, ul, "CALL Called Away (shares -)", -need)

                    cash = strike * 100.0 * qty
                    ledger_rows.append({
                        "Date": close_dt.date(),
                        "Underlying": ul,
                        "Event": "CALL Called Away (sell shares)",
                        "CashFlow": round(cash, 2),
                        "Note": f"Sell {need} @ {strike:.2f}",
                        "Warning": ""
                    })
                else:
                    # non abbastanza azioni
                    push_audit(close_dt, ul, "CALL Called Away (not enough shares)", 0)
                    ledger_rows.append({
                        "Date": close_dt.date(),
                        "Underlying": ul,
                        "Event": "CALL Called Away (sell shares)",
                        "CashFlow": 0.0,
                        "Note": f"WARNING: not enough shares (need {need}, have {have}) → cashflow skipped",
                        "Warning": "NOT_ENOUGH_SHARES"
                    })

            elif status == "Expired":
                # nessun extra flusso
                push_audit(close_dt, ul, f"{typ} Expired", 0)
                ledger_rows.append({
                    "Date": close_dt.date(),
                    "Underlying": ul,
                    "Event": f"{typ} Expired",
                    "CashFlow": 0.0,
                    "Note": "No extra cash; premium kept",
                    "Warning": ""
                })

    ledger = pd.DataFrame(ledger_rows, columns=ledger_cols)
    if not ledger.empty:
        ledger = ledger.sort_values(["Date", "Underlying", "Event"]).reset_index(drop=True)

    audit = pd.DataFrame(audit_rows, columns=audit_cols)
    if not audit.empty:
        audit = audit.sort_values(["Date", "Underlying"]).reset_index(drop=True)

    return ledger, audit

# -----------------------------
# Timeline di cassa e collateral
# -----------------------------
def cash_timeline_df(cash_ledger: pd.DataFrame, starting_cash: float) -> pd.DataFrame:
    """Cumulata cassa a partire dallo starting cash."""
    if cash_ledger is None or cash_ledger.empty:
        return pd.DataFrame(columns=["Date", "DailyFlow", "CumulativeCash"])
    agg = (cash_ledger.groupby("Date", as_index=False)["CashFlow"].sum()
           .rename(columns={"CashFlow": "DailyFlow"}))
    agg = agg.sort_values("Date").reset_index(drop=True)
    agg["CumulativeCash"] = float(starting_cash) + agg["DailyFlow"].cumsum()
    return agg

def csp_collateral_timeline(orders_df: pd.DataFrame) -> pd.DataFrame:
    """
    Timeline collateral PUT vendute:
      - Apertura: +collateral (strike*100*qty)
      - Chiusura (qualsiasi stato non-OPEN): -collateral
    """
    cols = ["Date", "CollateralChange", "CollateralOutstanding"]
    if orders_df is None or orders_df.empty:
        return pd.DataFrame(columns=cols)

    o = orders_df.copy()
    for c in ("OpenDate", "CloseDate", "Expiry"):
        if c in o.columns:
            o[c] = pd.to_datetime(o[c], errors="coerce")

    mask = (
        (o["Type"].astype(str).str.upper() == "PUT") &
        (o["Side"].astype(str).str.upper() == "SELL") &
        (pd.to_numeric(o.get("Qty", 0), errors="coerce").fillna(0) > 0) &
        (pd.to_numeric(o.get("Strike", 0), errors="coerce").fillna(0) > 0)
    )
    puts = o.loc[mask].copy()
    if puts.empty:
        return pd.DataFrame(columns=cols)

    puts["Qty"] = pd.to_numeric(puts["Qty"], errors="coerce").fillna(0).astype(int)
    puts["Strike"] = pd.to_numeric(puts["Strike"], errors="coerce").fillna(0.0)

    events = []
    for _, r in puts.iterrows():
        open_dt = r.get("OpenDate")
        close_dt = r.get("CloseDate")
        coll = float(r["Strike"]) * 100.0 * int(r["Qty"])

        if pd.notna(open_dt):
            events.append({"Date": open_dt.date(), "CollateralChange": +coll})
        if pd.notna(close_dt):
            events.append({"Date": close_dt.date(), "CollateralChange": -coll})

    if not events:
        return pd.DataFrame(columns=cols)

    ev = pd.DataFrame(events).groupby("Date", as_index=False)["CollateralChange"].sum()
    ev = ev.sort_values("Date").reset_index(drop=True)
    ev["CollateralOutstanding"] = ev["CollateralChange"].cumsum().clip(lower=0.0)
    return ev[["Date", "CollateralChange", "CollateralOutstanding"]]