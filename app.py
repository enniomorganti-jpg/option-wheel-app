# app.py
import streamlit as st
import asyncio
import locale
from database import load_table, save_table, load_settings, save_settings, wipe_all_data
from pricing import test_ibkr_connection, refresh_all_prices_yf, refresh_prices_ibkr, set_price_for, IB_AVAILABLE
from utils import positions_nonzero, clean_ticker
from sections import render_dashboard, render_portfolio, render_positions, render_analytics, render_cashflows
import config

# Setup
try:
    locale.setlocale(locale.LC_ALL, "it_IT.UTF-8")
except Exception:
    pass

# Async loop fix
try:
    asyncio.get_running_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

# App title
st.set_page_config(page_title="Option Wheel Strategy", layout="wide")
st.title("Wheel accounting")

# Sidebar
with st.sidebar:
    st.header("Settings (personal session)")

    # Crea un dizionario persistente nella sessione
    if "user_settings" not in st.session_state:
        st.session_state.user_settings = {
            "starting_cash": float(config.DEFAULT_STARTING_CASH),
            "ibkr_host": "127.0.0.1",
            "ibkr_port": 7497,
            "ibkr_client_id": 1,
            "ibkr_exchange_hints": {}
        }

    user_settings = st.session_state.user_settings

    # Starting capital (solo sessione corrente)
    user_settings["starting_cash"] = st.number_input(
        "Starting capital",
        value=float(user_settings["starting_cash"]),
        step=1000.0,
        help="Valore iniziale della cassa, salvato solo per questa sessione."
    )

    st.caption("Le impostazioni sono temporanee e isolate per ogni utente.")

    # Divider
    st.markdown("---")

    # IBKR Connection (solo sessione)
    st.subheader("IBKR Connection")
    user_settings["ibkr_host"] = st.text_input("Host", value=user_settings["ibkr_host"])
    user_settings["ibkr_port"] = st.number_input("Port", value=user_settings["ibkr_port"])
    user_settings["ibkr_client_id"] = st.number_input("Client ID", value=user_settings["ibkr_client_id"])

    col1, col2 = st.columns(2)
    if col1.button("Test IBKR Connection"):
        ok, info = test_ibkr_connection()
        if ok:
            st.success(f"Connected. Accounts: {', '.join(info.get('accounts', [])) or 'N/A'}")
        else:
            st.error(f"Connection failed: {info.get('error', 'Unknown error')}")

    if not IB_AVAILABLE:
        st.warning("IBKR integration (ib_insync) not installed or unavailable.")

    # Divider
    st.markdown("---")

    # Exchange Hints (sessione corrente)
    st.subheader("Exchange preferences")
    eh_symbol = st.text_input("Ticker").strip().upper()
    eh_exchange = st.selectbox("Preferred Exchange", ["NYSE", "NASDAQ", "ARCA", "SMART"])

    colh1, colh2 = st.columns(2)
    if colh1.button("Save hint"):
        if eh_symbol and eh_exchange:
            user_settings["ibkr_exchange_hints"][eh_symbol] = eh_exchange
            st.success(f"Hint saved: {eh_symbol} → {eh_exchange}")
        else:
            st.warning("Insert a ticker and select a valid exchange.")

    if colh2.button("Remove hint"):
        if eh_symbol in user_settings["ibkr_exchange_hints"]:
            del user_settings["ibkr_exchange_hints"][eh_symbol]
            st.success(f"Hint removed for {eh_symbol}.")
        else:
            st.info("No hint found for this ticker.")

    # Show active hints
    hints = user_settings.get("ibkr_exchange_hints", {})
    if hints:
        import pandas as pd
        st.caption("Current exchange preferences:")
        st.dataframe(
            pd.DataFrame([{"Ticker": k, "Exchange": v} for k, v in hints.items()])
              .sort_values("Ticker"),
            use_container_width=True,
            hide_index=True
        )

    # Divider
    st.markdown("---")
    st.caption("Session settings are temporary and not saved to disk.")
    # Exchange Hints
    st.markdown("---")
    st.subheader("Exchange Preferences")
    
    eh_symbol = st.text_input("Ticker", key="eh_sym").strip().upper()
    eh_exchange = st.selectbox("Exchange", ["NYSE", "NASDAQ", "ARCA", "SMART"], key="eh_ex")
    
    col1, col2 = st.columns(2)
    if col1.button("Save Hint"):
        if eh_symbol and eh_exchange:
            settings.setdefault("ibkr_exchange_hints", {})
            settings["ibkr_exchange_hints"][eh_symbol] = eh_exchange
            save_settings(settings)
            st.success(f"Saved: {eh_symbol} → {eh_exchange}")
        else:
            st.warning("Enter ticker and select exchange")
    
    if col2.button("Remove Hint"):
        if eh_symbol in settings.get("ibkr_exchange_hints", {}):
            del settings["ibkr_exchange_hints"][eh_symbol]
            save_settings(settings)
            st.success(f"Removed hint for {eh_symbol}")
        else:
            st.info("No hint found for this ticker")
    
    # Show current hints
    hints = settings.get("ibkr_exchange_hints", {})
    if hints:
        st.caption("Current preferences:")
        import pandas as pd
        st.dataframe(
            pd.DataFrame([{"Ticker": k, "Exchange": v} for k, v in hints.items()])
            .sort_values("Ticker"),
            use_container_width=True,
            hide_index=True
        )

# Main tabs
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Dashboard", 
    "New Order", 
    "Portfolio", 
    "Positions", 
    "Modify Orders", 
    "Analytics"
])

with tab1:
    render_dashboard(settings)

with tab2:
    st.subheader("New Option Order")
    
    orders = load_table("orders.csv", [])
    with st.form("new_order_form"):
        col1, col2, col3 = st.columns(3)
        underlying = col1.text_input("Underlying", placeholder="AAPL").strip().upper()
        option_type = col2.selectbox("Type", ["PUT", "CALL"])
        open_date = col3.date_input("Open Date")
        
        col4, col5, col6 = st.columns(3)
        expiry = col4.date_input("Expiry")
        strike = col5.number_input("Strike", min_value=0.0, step=0.5, format="%.2f")
        quantity = col6.number_input("Contracts", min_value=1, value=1)
        
        col7, col8, col9 = st.columns(3)
        premium = col7.number_input("Premium/Contract", min_value=0.0, step=0.05, format="%.2f")
        fees = col8.number_input("Fees", value=0.0, step=0.01, format="%.2f")
        delta = col9.number_input("Delta", value=0.0, step=0.01, format="%.2f")
        
        notes = st.text_input("Notes (optional)")
        
        if st.form_submit_button("Add Order"):
            if not underlying:
                st.error("Enter underlying symbol")
            elif premium <= 0:
                st.error("Premium must be > 0")
            else:
                from utils import next_order_id
                new_id = next_order_id(orders)
                new_order = {
                    "ID": new_id,
                    "Underlying": clean_ticker(underlying),
                    "Side": "Sell",
                    "Type": option_type,
                    "OpenDate": open_date,
                    "Expiry": expiry,
                    "Strike": float(strike),
                    "Qty": int(quantity),
                    "PricePerContract": float(premium),
                    "Fees": float(fees),
                    "Delta": float(delta),
                    "Notes": notes,
                    "Status": "Open",
                    "CloseDate": None
                }
                
                orders = pd.concat([orders, pd.DataFrame([new_order])], ignore_index=True)
                save_table(orders, "orders.csv")
                st.success(f"Order {new_id} added successfully!")
                st.rerun()

with tab3:
    render_portfolio()

with tab4:
    render_positions()

with tab5:
    st.subheader("Modify Orders")
    
    orders = load_table("orders.csv", [])
    if orders.empty:
        st.info("No orders to modify")
    else:
        scope = st.radio("Scope", ["Open Only", "All"], horizontal=True)
        view_data = orders.copy()
        
        if scope == "Open Only":
            view_data = view_data[view_data["Status"].astype(str).str.upper() == "OPEN"]
        
        if view_data.empty:
            st.caption("No orders in selected scope")
        else:
            # CORREZIONE COMPLETA: Prepara i dati per l'editor
            view_data = view_data.sort_values(["OpenDate", "ID"]).reset_index(drop=True)
            
            # Converti tutte le colonne problematiche
            view_data["Notes"] = view_data["Notes"].fillna("").astype(str)
            view_data["Underlying"] = view_data["Underlying"].astype(str)
            view_data["Side"] = view_data["Side"].astype(str)
            view_data["Type"] = view_data["Type"].astype(str)
            view_data["Status"] = view_data["Status"].astype(str)
            
            edited_data = st.data_editor(
                view_data,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ID": st.column_config.NumberColumn(disabled=True),
                    "Underlying": st.column_config.TextColumn(),
                    "Side": st.column_config.SelectboxColumn(options=["Sell", "Buy"]),
                    "Type": st.column_config.SelectboxColumn(options=["PUT", "CALL"]),
                    "Status": st.column_config.SelectboxColumn(
                        options=["Open", "Expired", "Assigned", "CalledAway", "Closed"]
                    ),
                    "OpenDate": st.column_config.DateColumn(),
                    "Expiry": st.column_config.DateColumn(),
                    "CloseDate": st.column_config.DateColumn(),
                    "Strike": st.column_config.NumberColumn(format="%.2f"),
                    "Qty": st.column_config.NumberColumn(),
                    "PricePerContract": st.column_config.NumberColumn(format="%.4f"),
                    "Fees": st.column_config.NumberColumn(format="%.4f"),
                    "Delta": st.column_config.NumberColumn(format="%.4f"),
                    "Notes": st.column_config.TextColumn(),
                }
            )
            
            if st.button("Save Changes"):
                # Processa i dati modificati
                edited_data["Underlying"] = edited_data["Underlying"].astype(str).map(clean_ticker)
                
                # Converti le colonne numeriche
                for col in ["Strike", "Qty", "PricePerContract", "Fees", "Delta"]:
                    edited_data[col] = pd.to_numeric(edited_data[col], errors="coerce")
                
                if scope == "Open Only":
                    edited_ids = set(edited_data["ID"])
                    other_orders = orders[~orders["ID"].isin(edited_ids)]
                    final_orders = pd.concat([other_orders, edited_data], ignore_index=True)
                else:
                    final_orders = edited_data
                
                final_orders = final_orders.sort_values(["OpenDate", "ID"]).reset_index(drop=True)
                save_table(final_orders, "orders.csv")
                st.success("Orders updated successfully!")
                st.rerun()
                
with tab6:
    render_analytics()

# Footer
st.markdown("---")
st.caption("Option Wheel Strategy Dashboard - Modular Version")
