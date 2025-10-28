# sections/positions.py
import streamlit as st
import pandas as pd
from datetime import date
from database import load_table, save_table
from utils import clean_ticker, next_order_id, covered_contracts_available, positions_nonzero, format_currency  # AGGIUNTA format_currency
from pricing import get_price_for, set_price_for

def render_positions():
    st.subheader("Stock Positions")
    
    positions = load_table("positions.csv", [])
    orders = load_table("orders.csv", [])
    
    # Show only active positions
    active_positions = positions_nonzero(positions)
    
    if active_positions.empty:
        st.info("No active positions.")
        return

    # Display positions with current prices
    pos_view = active_positions.copy()
    pos_view["Underlying"] = pos_view["Underlying"].astype(str).map(clean_ticker)
    pos_view["AvgCost"] = pd.to_numeric(pos_view["AvgCost"], errors="coerce").fillna(0.0).round(4)
    pos_view["Last"] = pos_view["Underlying"].apply(lambda u: get_price_for(u) or 0.0)
    pos_view["MarketValue"] = (pos_view["Qty"] * pos_view["Last"].fillna(0)).round(2)

    # FIX: Formatta le colonne numeriche nel dataframe
    display_pos_view = pos_view.copy()
    display_pos_view["AvgCost"] = display_pos_view["AvgCost"].apply(format_currency)
    display_pos_view["Last"] = display_pos_view["Last"].apply(format_currency)
    display_pos_view["MarketValue"] = display_pos_view["MarketValue"].apply(format_currency)

    st.dataframe(
        display_pos_view[["Underlying", "Qty", "AvgCost", "Last", "MarketValue"]]
        .rename(columns={
            "Underlying": "Ticker", "Qty": "Shares", "AvgCost": "Avg Cost",
            "Last": "Last", "MarketValue": "Value"
        })
        .sort_values("Value", ascending=False),
        use_container_width=True
    )

    # Manual price update
    st.markdown("---")
    st.subheader("Manual Price Update")
    ul_list = sorted(pos_view["Underlying"].unique().tolist())
    col1, col2, col3 = st.columns(3)
    selected_ul = col1.selectbox("Ticker", ul_list)
    current_price = get_price_for(selected_ul) or 0.0
    # FIX: Mostra il prezzo corrente formattato
    col2.metric("Current Price", format_currency(current_price))
    new_price = col3.number_input("New Price", value=float(current_price), step=0.01, format="%.2f")
    
    if st.button("Save Price", key="save_price_btn"):
        set_price_for(selected_ul, new_price)
        st.success(f"Price updated for {selected_ul}.")
        st.rerun()

    # Covered call selling
    st.markdown("---")
    st.subheader("Sell Covered Call")
    
    with st.form("sell_covered_call"):
        selected_ul_cc = st.selectbox("Underlying", ul_list, key="cc_ul")
        max_covered = covered_contracts_available(positions, selected_ul_cc, orders)
        
        col1, col2, col3, col4, col5 = st.columns(5)
        
        qty_contracts = col1.number_input(
            f"Contracts (max {max_covered})",
            min_value=1,
            max_value=max_covered if max_covered > 0 else 1,
            value=1,
            step=1,
            disabled=(max_covered == 0)
        )
        strike = col2.number_input("Strike", value=0.0, step=0.5, format="%.2f")
        expiry = col3.date_input("Expiry", value=None, format="YYYY-MM-DD")
        # FIX: Aggiunto formato al premium
        premium = col4.number_input("Premium per contract", value=0.0, step=0.05, format="%.2f")
        delta = col5.number_input("Delta (optional)", value=0.0, step=0.01, format="%.2f")

        # FIX: Mostra informazioni aggiuntive formattate
        if max_covered > 0:
            st.info(f"Available covered slots: {max_covered} contracts")
        
        if st.form_submit_button("Add Covered Call to Portfolio"):
            if max_covered == 0:
                st.error("No shares available for covered calls on this ticker.")
            elif premium <= 0:
                st.error("Enter a premium > 0.")
            elif strike <= 0:
                st.error("Enter a strike price > 0.")
            elif expiry is None:
                st.error("Select an expiry date.")
            else:
                new_order = {
                    "ID": next_order_id(orders),
                    "Underlying": clean_ticker(selected_ul_cc),
                    "Side": "Sell",
                    "Type": "CALL",
                    "OpenDate": date.today(),
                    "Expiry": expiry if expiry else None,
                    "Strike": float(strike),
                    "Qty": int(qty_contracts),
                    "PricePerContract": float(premium),
                    "Fees": 0.0,
                    "Delta": float(delta),
                    "Notes": "CC from Positions",
                    "Status": "Open",
                    "CloseDate": None,
                }
                
                orders = pd.concat([orders, pd.DataFrame([new_order])], ignore_index=True)
                save_table(orders, "orders.csv")
                # FIX: Formatta il messaggio di successo
                total_premium = float(premium) * int(qty_contracts)
                st.success(f"Covered call added (ID {new_order['ID']}) - Total Premium: {format_currency(total_premium)}")
                st.rerun()