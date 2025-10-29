# sections/portfolio.py
import streamlit as st
import pandas as pd
from datetime import date, datetime

from database import load_table, save_table
from utils import clean_ticker, format_currency
from analytics import compute_order_metrics, compute_calledaway_pl_row


# ------------------------------
# Helpers posizioni (self-contained)
# ------------------------------
def _add_shares_avg_cost(positions_df: pd.DataFrame, ul: str, qty_shares: int, net_price_per_share: float) -> pd.DataFrame:
    """Aggiunge azioni aggiornando il costo medio."""
    ulc = clean_ticker(ul)
    df = positions_df.copy()

    if df.empty or "Underlying" not in df.columns:
        # crea struttura minima
        df = pd.DataFrame(columns=["Underlying", "Qty", "AvgCost"])

    row = df.loc[df["Underlying"].astype(str).str.upper() == ulc]
    if row.empty:
        df = pd.concat([df, pd.DataFrame([{
            "Underlying": ulc,
            "Qty": int(qty_shares),
            "AvgCost": float(round(net_price_per_share, 4))
        }])], ignore_index=True)
    else:
        old_qty = int(pd.to_numeric(row["Qty"].values[0], errors="coerce") or 0)
        old_avg = float(pd.to_numeric(row["AvgCost"].values[0], errors="coerce") or 0.0)
        new_qty = old_qty + int(qty_shares)
        if new_qty <= 0:
            df.loc[df["Underlying"].astype(str).str.upper() == ulc, ["Qty", "AvgCost"]] = [0, 0.0]
        else:
            new_avg = (old_qty * old_avg + int(qty_shares) * float(net_price_per_share)) / new_qty
            df.loc[df["Underlying"].astype(str).str.upper() == ulc, ["Qty", "AvgCost"]] = [int(new_qty), float(round(new_avg, 4))]
    return df


def _remove_shares(positions_df: pd.DataFrame, ul: str, shares_to_remove: int) -> pd.DataFrame:
    """Rimuove azioni (per Called Away)."""
    ulc = clean_ticker(ul)
    df = positions_df.copy()

    if df.empty or "Underlying" not in df.columns:
        # niente da togliere
        return df

    row = df.loc[df["Underlying"].astype(str).str.upper() == ulc]
    if row.empty:
        return df
    old_qty = int(pd.to_numeric(row["Qty"].values[0], errors="coerce") or 0)
    new_qty = max(0, old_qty - int(shares_to_remove))
    df.loc[df["Underlying"].astype(str).str.upper() == ulc, "Qty"] = int(new_qty)
    if new_qty == 0:
        df.loc[df["Underlying"].astype(str).str.upper() == ulc, "AvgCost"] = 0.0
    return df


# ------------------------------
# Piccoli helper UI
# ------------------------------
def _fmt_date(dt):
    """Formatta la data in DD/MM/YYYY per display."""
    if pd.isna(dt) or dt is None:
        return "—"
    try:
        return pd.to_datetime(dt).date().strftime("%d/%m/%Y")
    except Exception:
        return "—"


def _calculate_dte(expiry_date):
    """Calcola Days To Expiration per display."""
    if pd.isna(expiry_date) or expiry_date is None:
        return "—"
    try:
        today = date.today()
        dte = (pd.to_datetime(expiry_date).date() - today).days
        return f"{max(0, dte)} gg"
    except Exception:
        return "—"


def _fmt_pct_local(x):
    """Percentuali in formato italiano."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        return f"{float(x)*100:,.2f}%".replace(",", "§").replace(".", ",").replace("§", ".")
    except Exception:
        return "—"


# ------------------------------
# Date input robusti (anti-crash Cloud)
# ------------------------------
def _to_pydate(x):
    """Converte qualsiasi input (Timestamp/str/datetime) in datetime.date oppure None."""
    if x is None:
        return None
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    try:
        xd = pd.to_datetime(x, errors="coerce")
        if pd.isna(xd):
            return None
        return xd.date()
    except Exception:
        return None


def safe_date_input(label: str, value, min_value=None, max_value=None, key=None):
    """
    st.date_input robusto:
    - coercizza a date
    - se min>max rimuove i bound
    - clamp del value dentro i limiti
    """
    v = _to_pydate(value) or date.today()
    mn = _to_pydate(min_value)
    mx = _to_pydate(max_value)

    if mn and mx and mn > mx:
        mn, mx = None, None

    if mn and v < mn:
        v = mn
    if mx and v > mx:
        v = mx

    kwargs = {"label": label, "value": v, "key": key}
    if mn is not None:
        kwargs["min_value"] = mn
    if mx is not None:
        kwargs["max_value"] = mx
    return st.date_input(**kwargs)


# ------------------------------
# ACTION HANDLERS con DATA SCELTA
# ------------------------------
def _handle_put_expire(order_id, orders, action_date):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
        return
    orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Expired", action_date]
    save_table(orders, "orders.csv")
    st.success(f"PUT ID {order_id} marked as Expired on {action_date}.")
    st.rerun()


def _handle_put_assign(order_id, row, orders, positions, action_date):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
        return

    qty_contracts = int(pd.to_numeric(row["Qty"], errors="coerce") or 0)
    qty_shares = qty_contracts * 100
    strike = float(pd.to_numeric(row["Strike"], errors="coerce") or 0.0)
    premium = float(pd.to_numeric(row["PricePerContract"], errors="coerce") or 0.0)
    net_price = strike - premium
    ul = clean_ticker(row["Underlying"])

    positions_local = _add_shares_avg_cost(positions, ul, qty_shares, net_price)
    save_table(positions_local, "positions.csv")

    orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Assigned", action_date]
    save_table(orders, "orders.csv")

    st.success(f"PUT ID {order_id} assigned on {action_date}: +{qty_shares} shares @ {format_currency(net_price)}.")
    st.rerun()


def _handle_call_expire(order_id, orders, action_date):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
        return
    orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Expired", action_date]
    save_table(orders, "orders.csv")
    st.success(f"CALL ID {order_id} marked as Expired on {action_date}.")
    st.rerun()


def _handle_call_called_away(order_id, row, orders, positions, realized, action_date):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
        return

    qty_contracts = int(pd.to_numeric(row["Qty"], errors="coerce") or 0)
    shares = qty_contracts * 100
    strike = float(pd.to_numeric(row["Strike"], errors="coerce") or 0.0)
    premium = float(pd.to_numeric(row["PricePerContract"], errors="coerce") or 0.0)
    fees = float(pd.to_numeric(row.get("Fees", 0.0), errors="coerce") or 0.0)
    ul = clean_ticker(row["Underlying"])

    if positions.empty or "Underlying" not in positions.columns:
        st.error(f"No positions found for {ul}. Cannot deliver shares.")
        return

    pos_row = positions.loc[positions["Underlying"].astype(str).str.upper() == ul]
    if pos_row.empty or int(pd.to_numeric(pos_row["Qty"].values[0], errors="coerce") or 0) < shares:
        st.error("Insufficient shares for assignment.")
        return

    avg_cost = float(pd.to_numeric(pos_row["AvgCost"].values[0], errors="coerce") or 0.0)
    equity_pl, total_pl = compute_calledaway_pl_row(
        assigned_avg_cost=avg_cost,
        strike_call=strike,
        premium_call=premium,
        shares=shares,
        fees=fees
    )

    # Update positions (remove shares)
    positions_local = _remove_shares(positions, ul, shares)
    save_table(positions_local, "positions.csv")

    # Append realized row
    if realized is None or realized.empty:
        realized_local = pd.DataFrame(columns=[
            "Underlying", "Event", "EventDate", "Shares", "Strike",
            "PremiumPerShare", "AvgCostAtEvent", "EquityPL", "TotalPL", "Notes"
        ])
    else:
        realized_local = realized.copy()

    new_realized = pd.DataFrame([{
        "Underlying": ul,
        "Event": "CalledAway",
        "EventDate": action_date,
        "Shares": shares,
        "Strike": strike,
        "PremiumPerShare": premium,
        "AvgCostAtEvent": avg_cost,
        "EquityPL": equity_pl,
        "TotalPL": total_pl,
        "Notes": str(row.get("Notes", "")) if "Notes" in row else ""
    }])
    realized_local = pd.concat([realized_local, new_realized], ignore_index=True)
    save_table(realized_local, "realized.csv")

    # Update order
    orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["CalledAway", action_date]
    save_table(orders, "orders.csv")

    st.success(f"CALL ID {order_id} called away on {action_date}. Total P/L = {format_currency(total_pl)}.")
    st.rerun()


# ------------------------------
# RENDER
# ------------------------------
def render_portfolio():
    st.subheader("Portfolio")

    orders = load_table("orders.csv", [])
    positions = load_table("positions.csv", [])
    realized = load_table("realized.csv", [])

    if orders is None or orders.empty:
        st.info("No orders in portfolio.")
        return

    # filtro open vs storico
    show_hist = st.toggle("Show also historical (Expired / Assigned / CalledAway / Closed)", value=False)
    view = orders.copy()
    if not show_hist:
        view = view[view["Status"].astype(str).str.upper() == "OPEN"]

    if view.empty:
        st.caption("No orders in selected filter.")
        return

    # normalizza date per ordinamento
    for c in ("OpenDate", "Expiry", "CloseDate"):
        if c in view.columns:
            view[c] = pd.to_datetime(view[c], errors="coerce")
    view = view.sort_values(["Expiry", "OpenDate", "ID"], ascending=[True, True, True]).reset_index(drop=True)

    # per ogni ordine: card + date picker + bottoni
    for _, row in view.iterrows():
        metrics = compute_order_metrics(row)
        order_id = int(row["ID"])
        ul = clean_ticker(row["Underlying"])
        option_type = str(row["Type"]).upper()

        status_live = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).strip()
        is_open = (status_live.upper() == "OPEN")

        open_dt  = pd.to_datetime(row.get("OpenDate"), errors="coerce")
        expiry_dt = pd.to_datetime(row.get("Expiry"), errors="coerce")
        close_dt = pd.to_datetime(row.get("CloseDate"), errors="coerce")

        open_txt   = _fmt_date(open_dt)
        expiry_txt = _fmt_date(expiry_dt)
        closed_txt = _fmt_date(close_dt)
        dte_txt    = _calculate_dte(expiry_dt)

        status = str(row.get("Status", "")).strip()
        status_descr = {
            "OPEN": "Open",
            "EXPIRED": "Expired",
            "ASSIGNED": "Assigned",
            "CALLEDAWAY": "CalledAway",
            "CLOSED": "Closed"
        }.get(status.upper(), status or "n/d")

        with st.container(border=True):
            # Header
            c1, c2, c3, c4, c5, c6 = st.columns([2, 2, 2, 2, 2, 2])
            c1.markdown(f"**{ul} — {option_type}**")
            c2.write(f"Strike: {format_currency(row['Strike'])}")
            c3.write(f"Contracts: {int(pd.to_numeric(row['Qty'], errors='coerce') or 0)}")
            c4.write(f"Premium: {format_currency(row['PricePerContract'])}")
            c5.write(f"Delta: {float(pd.to_numeric(row.get('Delta', 0), errors='coerce') or 0.0):.2f}")
            c6.write(f"Status: {status_descr}")

            if is_open:
                st.markdown(f"**Opened on:** {open_txt} | **Expiration:** {expiry_txt} | **DTE:** {dte_txt}")
            else:
                st.markdown(f"**Opened on:** {open_txt} | **Expiration:** {expiry_txt} | **Closed on:** {closed_txt}")

            # Metrics riga
            m1, m2, m3, m4 = st.columns([2, 2, 2, 2])
            m1.write(f"**Income:** {format_currency(metrics['income'])}")
            m2.write(f"**ROI:** {_fmt_pct_local(metrics['roi'])}")
            m3.write(f"**Annualized ROI:** {_fmt_pct_local(metrics['ann_roi'])}")
            m4.write(f"**Prob. ITM:** {_fmt_pct_local(metrics['prob_itm'])}")

            # --- Issuance/Open date (retrodatare la vendita) ---
            iss_col, act_col = st.columns([1, 1])

            iss_default = open_dt if pd.notna(open_dt) else date.today()
            new_open_date = safe_date_input(
                "Issuance / Open date",
                value=iss_default,
                key=f"iss_{order_id}",
            )
            if (pd.notna(open_dt) and new_open_date != open_dt.date()) or (pd.isna(open_dt) and new_open_date is not None):
                if iss_col.button("Save issuance date", key=f"save_iss_{order_id}"):
                    orders.loc[orders["ID"] == order_id, "OpenDate"] = pd.to_datetime(new_open_date)
                    save_table(orders, "orders.csv")
                    st.success(f"Issuance/Open date updated to {new_open_date} for order {order_id}.")
                    st.rerun()

            # --- Action date (anti-crash, bound riparati + clamp) ---
            default_action_date = expiry_dt if pd.notna(expiry_dt) else date.today()
            min_date = open_dt if pd.notna(open_dt) else None
            max_date = expiry_dt if pd.notna(expiry_dt) else None

            action_date = safe_date_input(
                "Set the action date",
                value=default_action_date,
                min_value=min_date,
                max_value=max_date,
                key=f"act_date_{order_id}",
            )

            # --- Bottoni ---
            if is_open:
                a1, a2 = st.columns(2)

                if option_type == "PUT":
                    expire_clicked = a1.button("Expire", key=f"exp_put_{order_id}")
                    assign_clicked = a2.button("Assign", key=f"ass_put_{order_id}")

                    if expire_clicked:
                        _handle_put_expire(order_id, orders, action_date)

                    if assign_clicked:
                        _handle_put_assign(order_id, row, orders, positions, action_date)

                elif option_type == "CALL":
                    expire_clicked = a1.button("Expire", key=f"exp_call_{order_id}")
                    called_clicked = a2.button("Called Away", key=f"called_{order_id}")

                    if expire_clicked:
                        _handle_call_expire(order_id, orders, action_date)

                    if called_clicked:
                        _handle_call_called_away(order_id, row, orders, positions, realized, action_date)

                else:
                    st.caption("Only PUT/CALL supported here.")
