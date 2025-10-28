# sections/portfolio.py
import streamlit as st
import pandas as pd
from datetime import date
from database import load_table, save_table
from utils import clean_ticker, next_order_id, format_currency  # AGGIUNTA format_currency
from analytics import compute_order_metrics, compute_calledaway_pl_row

def render_portfolio():
    st.subheader("Portfolio")
    
    orders = load_table("orders.csv", [])
    positions = load_table("positions.csv", [])
    realized = load_table("realized.csv", [])
    
    def _fmt_date(dt):
        """Formatta la data in DD/MM/YYYY"""
        if pd.isna(dt) or dt is None:
            return "—"
        try:
            return dt.strftime("%d/%m/%Y")
        except Exception:
            return "—"

    def _calculate_dte(expiry_date):
        """Calcola Days To Expiration"""
        if pd.isna(expiry_date) or expiry_date is None:
            return "—"
        try:
            today = date.today()
            dte = (expiry_date - today).days
            return f"{max(0, dte)} gg"
        except Exception:
            return "—"

    def _fmt_pct_local(x):
        """Formatta le percentuali in formato italiano"""
        if x is None or pd.isna(x):
            return "—"
        try:
            # Converti in percentuale e formatta con virgola
            return f"{float(x)*100:,.2f}%".replace(",", "§").replace(".", ",").replace("§", ".")
        except Exception:
            return "—"

    if orders.empty:
        st.info("No orders in portfolio.")
        return

    show_hist = st.toggle("Show also historical (Expired / Assigned / CalledAway / Closed)", value=False)
    view = orders.copy()
    if not show_hist:
        view = view[view["Status"].astype(str).str.upper() == "OPEN"]

    if view.empty:
        st.caption("No orders in selected filter.")
        return

    view = view.sort_values(["Expiry", "OpenDate", "ID"], ascending=[True, True, True]).reset_index(drop=True)
    
    for _, row in view.iterrows():
        metrics = compute_order_metrics(row)
        order_id = int(row["ID"])
        ul = clean_ticker(row["Underlying"])
        option_type = str(row["Type"]).upper()

        status_live = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).strip()
        is_open = (status_live.upper() == "OPEN")

        open_dt = pd.to_datetime(row.get("OpenDate"), errors="coerce")
        expiry_dt = pd.to_datetime(row.get("Expiry"), errors="coerce")
        close_dt = pd.to_datetime(row.get("CloseDate"), errors="coerce")
        
        # NUOVO FORMATO DATE
        open_txt = _fmt_date(open_dt)
        expiry_txt = _fmt_date(expiry_dt) 
        closed_txt = _fmt_date(close_dt)
        dte_txt = _calculate_dte(expiry_dt)

        status = str(row.get("Status","")).strip()
        status_descr = {
            "OPEN": "Open",
            "EXPIRED": "Expired",
            "ASSIGNED": "Assigned",
            "CALLEDAWAY": "CalledAway",
            "CLOSED": "Closed"
        }.get(status.upper(), status or "n/d")

        with st.container(border=True):
            # Header row
            c1, c2, c3, c4, c5, c6 = st.columns([2, 2, 2, 2, 2, 2])
            c1.markdown(f"**{ul} — {option_type}**")
            # FIX: Formatta Strike e Premium
            c2.write(f"Strike: {format_currency(row['Strike'])}")
            c3.write(f"Contracts: {int(row['Qty'])}")
            c4.write(f"Premium: {format_currency(row['PricePerContract'])}")
            c5.write(f"Delta: {float(row.get('Delta', 0)):.2f}")
            c6.write(f"Status: {status_descr}")

            # NUOVO BANNER CON DATE MIGLIORATE
            if is_open:
                st.markdown(f"**Opened on:** {open_txt} | **Expiration:** {expiry_txt} | **DTE:** {dte_txt}")
            else:
                st.markdown(f"**Opened on:** {open_txt} | **Expiration:** {expiry_txt} | **Closed on:** {closed_txt}")

            # Metrics row - RIMOSSO "1d" FINALE
            m1, m2, m3, m4 = st.columns([2, 2, 2, 2])
            # FIX: Formatta Income
            m1.write(f"**Income:** {format_currency(metrics['income'])}")
            m2.write(f"**ROI:** {_fmt_pct_local(metrics['roi'])}")
            m3.write(f"**Annualized ROI:** {_fmt_pct_local(metrics['ann_roi'])}")
            m4.write(f"**Prob. ITM:** {_fmt_pct_local(metrics['prob_itm'])}")

            # Action buttons
            if is_open:
                a1, a2 = st.columns(2)
                
                if option_type == "PUT":
                    expire_clicked = a1.button("Expire", key=f"exp_put_{order_id}")
                    assign_clicked = a2.button("Assign", key=f"ass_put_{order_id}")

                    if expire_clicked:
                        _handle_put_expire(order_id, orders)
                        
                    if assign_clicked:
                        _handle_put_assign(order_id, row, orders, positions)

                elif option_type == "CALL":
                    expire_clicked = a1.button("Expire", key=f"exp_call_{order_id}")
                    called_clicked = a2.button("Called Away", key=f"called_{order_id}")

                    if expire_clicked:
                        _handle_call_expire(order_id, orders)
                        
                    if called_clicked:
                        _handle_call_assign(order_id, row, orders, positions, realized)

def _handle_put_expire(order_id, orders):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
    else:
        orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Expired", date.today()]
        save_table(orders, "orders.csv")
        st.success(f"PUT ID {order_id} marked as Expired.")
        st.rerun()

def _handle_put_assign(order_id, row, orders, positions):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
    else:
        qty_contracts = int(row["Qty"])
        qty_shares = qty_contracts * 100
        strike = float(row["Strike"])
        premium = float(row["PricePerContract"])
        net_price = strike - premium
        ul = clean_ticker(row["Underlying"])

        # CORREZIONE: Verifica che positions abbia la colonna Underlying
        if positions.empty or "Underlying" not in positions.columns:
            # Se positions è vuoto o non ha la colonna, crea un nuovo DataFrame
            new_row = pd.DataFrame([{
                "Underlying": ul, "Qty": qty_shares, "AvgCost": round(net_price, 4)
            }])
            positions = pd.concat([positions, new_row], ignore_index=True)
        else:
            # Cerca la posizione esistente
            pos_row = positions.loc[positions["Underlying"].astype(str).str.upper() == ul]
            
            if not pos_row.empty:
                old_qty = int(pos_row["Qty"].values[0])
                old_avg = float(pos_row["AvgCost"].values[0])
                new_qty = old_qty + qty_shares
                new_avg = (old_qty * old_avg + qty_shares * net_price) / new_qty
                positions.loc[positions["Underlying"].astype(str).str.upper() == ul, ["Qty", "AvgCost"]] = [new_qty, round(new_avg, 4)]
            else:
                new_row = pd.DataFrame([{
                    "Underlying": ul, "Qty": qty_shares, "AvgCost": round(net_price, 4)
                }])
                positions = pd.concat([positions, new_row], ignore_index=True)

        # Update order
        orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Assigned", date.today()]
        
        save_table(positions, "positions.csv")
        save_table(orders, "orders.csv")
        
        # FIX: Formatta il messaggio di successo
        st.success(f"PUT ID {order_id} assigned: +{qty_shares} shares @ {format_currency(net_price)}.")
        st.rerun()

def _handle_call_expire(order_id, orders):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
    else:
        orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["Expired", date.today()]
        save_table(orders, "orders.csv")
        st.success(f"CALL ID {order_id} marked as Expired.")
        st.rerun()

def _handle_call_assign(order_id, row, orders, positions, realized):
    status_now = str(orders.loc[orders["ID"] == order_id, "Status"].iloc[0]).upper()
    if status_now != "OPEN":
        st.info("Order already not-OPEN, no action.")
    else:
        qty_contracts = int(row["Qty"])
        shares = qty_contracts * 100
        strike = float(row["Strike"])
        premium = float(row["PricePerContract"])
        fees = float(row.get("Fees", 0.0) or 0.0)
        ul = clean_ticker(row["Underlying"])

        # CORREZIONE: Verifica che positions abbia la colonna Underlying e dati
        if positions.empty or "Underlying" not in positions.columns:
            st.error(f"No positions found for {ul}. Cannot assign CALL.")
            return

        pos_row = positions.loc[positions["Underlying"].astype(str).str.upper() == ul]
        if pos_row.empty or int(pos_row["Qty"].values[0]) < shares:
            st.error("Insufficient shares for assignment.")
        else:
            avg_cost = float(pos_row["AvgCost"].values[0])
            equity_pl, total_pl = compute_calledaway_pl_row(avg_cost, strike, premium, shares, fees)

            # Update positions
            old_qty = int(pos_row["Qty"].values[0])
            new_qty = max(0, old_qty - shares)
            positions.loc[positions["Underlying"].astype(str).str.upper() == ul, "Qty"] = new_qty
            if new_qty == 0:
                positions.loc[positions["Underlying"].astype(str).str.upper() == ul, "AvgCost"] = 0.0

            # Add to realized
            new_realized = pd.DataFrame([{
                "Underlying": ul, "Event": "CalledAway", "EventDate": date.today(),
                "Shares": shares, "Strike": strike, "PremiumPerShare": premium,
                "AvgCostAtEvent": avg_cost, "EquityPL": equity_pl, "TotalPL": total_pl,
                "Notes": str(row.get("Notes", ""))  # Converti in stringa
            }])
            realized = pd.concat([realized, new_realized], ignore_index=True)

            # Update order
            orders.loc[orders["ID"] == order_id, ["Status", "CloseDate"]] = ["CalledAway", date.today()]

            save_table(positions, "positions.csv")
            save_table(realized, "realized.csv")
            save_table(orders, "orders.csv")

            # FIX: Formatta il messaggio di successo
            st.success(f"CALL ID {order_id} called away. Total P/L = {format_currency(total_pl)}.")
            st.rerun()