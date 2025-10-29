# app.py
import streamlit as st
import pandas as pd
import asyncio
import locale
import pyrebase

import database
from database import load_table, save_table, wipe_all_data
from pricing import test_ibkr_connection, IB_AVAILABLE
from utils import positions_nonzero, clean_ticker
from sections import (
    render_dashboard,
    render_portfolio,
    render_positions,
    render_analytics,
    render_cashflows,
)
import config

# =========================
# Setup locale + asyncio
# =========================
try:
    locale.setlocale(locale.LC_ALL, "it_IT.UTF-8")
except Exception:
    pass

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

# =========================
# App meta
# =========================
st.set_page_config(page_title="Option Wheel Strategy", layout="wide")
st.title("Wheel accounting")

_PYREBASE_APP = None


def _init_pyrebase_app():
    global _PYREBASE_APP
    if _PYREBASE_APP is not None:
        return _PYREBASE_APP
    if "firebase" not in st.secrets:
        raise RuntimeError("Firebase configuration missing in Streamlit secrets.")
    firebase_config = {k: v for k, v in st.secrets["firebase"].items()}
    _PYREBASE_APP = pyrebase.initialize_app(firebase_config)
    return _PYREBASE_APP


def _firebase_auth():
    try:
        return _init_pyrebase_app().auth()
    except Exception as exc:
        st.sidebar.error(f"Impossibile inizializzare Firebase: {exc}")
        st.stop()


def _format_auth_error(exc: Exception) -> str:
    message = str(exc)
    if hasattr(exc, "args") and exc.args:
        for arg in exc.args:
            if isinstance(arg, dict):
                message = arg.get("error", {}).get("message", message)
            elif isinstance(arg, str):
                message = arg
    return message.replace("_", " ")

# =========================
# Sidebar (session-based)
# =========================
with st.sidebar:
    st.header("Account")
    auth = _firebase_auth()
    current_user = st.session_state.get("user")

    if current_user:
        database.set_current_user(current_user["uid"])
        st.success(f"Logged in as {current_user.get('email', 'utente')}")
        if st.button("Logout"):
            st.session_state.user = None
            database.set_current_user(None)
            st.rerun()
    else:
        st.caption("Accedi o crea un account per sincronizzare i dati della ruota.")
        auth_mode = st.radio("Azione", ["Login", "Signup"], horizontal=True, key="auth_mode")
        with st.form("auth_form"):
            email = st.text_input("Email", key="auth_email").strip()
            password = st.text_input("Password", type="password", key="auth_password")
            confirm = None
            if auth_mode == "Signup":
                confirm = st.text_input("Conferma Password", type="password", key="auth_password_confirm")
            submit = st.form_submit_button("Login" if auth_mode == "Login" else "Crea account")

        if submit:
            if not email or not password:
                st.warning("Inserisci email e password.")
            elif auth_mode == "Signup" and password != (confirm or ""):
                st.warning("Le password non coincidono.")
            else:
                try:
                    if auth_mode == "Signup":
                        auth.create_user_with_email_and_password(email, password)
                    user_creds = auth.sign_in_with_email_and_password(email, password)
                    st.session_state.user = {
                        "uid": user_creds.get("localId"),
                        "email": user_creds.get("email", email),
                        "idToken": user_creds.get("idToken"),
                    }
                    database.set_current_user(st.session_state.user["uid"])
                    st.success("Autenticazione completata.")
                    st.rerun()
                except Exception as exc:
                    message = _format_auth_error(exc)
                    st.error(f"Autenticazione fallita: {message}")

    if "user" not in st.session_state or st.session_state.user is None:
        st.info("Efettua il login per continuare.")
        st.stop()

    st.markdown("---")
    st.header("Settings (personal session)")

    if "user_settings" not in st.session_state:
        st.session_state.user_settings = {
            "starting_cash": float(config.DEFAULT_STARTING_CASH),
            "ibkr_host": "127.0.0.1",
            "ibkr_port": 7497,
            "ibkr_client_id": 1,
            "ibkr_exchange_hints": {},
        }

    user_settings = st.session_state.user_settings

    user_settings["starting_cash"] = st.number_input(
        "Starting capital",
        value=float(user_settings["starting_cash"]),
        step=1000.0,
        help="Valore iniziale della cassa, salvato solo per questa sessione.",
    )

    st.caption("Le impostazioni sono temporanee e isolate per ogni utente.")
    st.markdown("---")

    st.subheader("IBKR Connection")
    user_settings["ibkr_host"] = st.text_input("Host", value=user_settings["ibkr_host"])
    user_settings["ibkr_port"] = st.number_input("Port", value=int(user_settings["ibkr_port"]))
    user_settings["ibkr_client_id"] = st.number_input("Client ID", value=int(user_settings["ibkr_client_id"]))

    col1, col2 = st.columns(2)
    if col1.button("Test IBKR Connection"):
        ok, info = test_ibkr_connection()
        if ok:
            st.success(f"Connected. Accounts: {', '.join(info.get('accounts', [])) or 'N/A'}")
        else:
            st.error(f"Connection failed: {info.get('error', 'Unknown error')}")

    if not IB_AVAILABLE:
        st.warning("ib_insync non disponibile in questo ambiente.")

    st.markdown("---")

    st.subheader("Exchange preferences")
    eh_symbol = st.text_input("Ticker").strip().upper()
    eh_exchange = st.selectbox("Preferred Exchange", ["NYSE", "NASDAQ", "ARCA", "SMART"])

    colh1, colh2 = st.columns(2)
    if colh1.button("Save hint"):
        if eh_symbol and eh_exchange:
            user_settings["ibkr_exchange_hints"][eh_symbol] = eh_exchange
            st.success(f"Hint saved: {eh_symbol} → {eh_exchange}")
        else:
            st.warning("Inserisci un ticker e seleziona un exchange.")

    if colh2.button("Remove hint"):
        if eh_symbol in user_settings["ibkr_exchange_hints"]:
            del user_settings["ibkr_exchange_hints"][eh_symbol]
            st.success(f"Hint removed for {eh_symbol}.")
        else:
            st.info("No hint found for this ticker.")

    hints = user_settings.get("ibkr_exchange_hints", {})
    if hints:
        st.caption("Current exchange preferences:")
        st.dataframe(
            pd.DataFrame([{"Ticker": k, "Exchange": v} for k, v in hints.items()]).sort_values("Ticker"),
            width="stretch",
            hide_index=True,
        )

    st.markdown("---")
    st.subheader("Danger zone")
    if st.button("Erase ALL Data"):
        if st.checkbox("Confermo di voler cancellare tutti i dati"):
            wipe_all_data()
            st.success("Dati cancellati. Ricarico l'app…")
            st.rerun()

# Questo dict viene passato alla dashboard
settings_session = {
    "starting_cash": float(st.session_state.user_settings["starting_cash"]),
    "ibkr_host": st.session_state.user_settings["ibkr_host"],
    "ibkr_port": int(st.session_state.user_settings["ibkr_port"]),
    "ibkr_client_id": int(st.session_state.user_settings["ibkr_client_id"]),
    "ibkr_exchange_hints": dict(st.session_state.user_settings.get("ibkr_exchange_hints", {})),
}

# =========================
# Main tabs
# =========================
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["Dashboard", "New Order", "Portfolio", "Positions", "Modify Orders", "Analytics"]
)

with tab1:
    # La dashboard usa starting_cash dalla sessione
    render_dashboard(settings_session)

with tab2:
    st.subheader("New Option Order")
    orders = load_table("orders.csv", [])

    with st.form("new_order_form"):
        c1, c2, c3 = st.columns(3)
        underlying = c1.text_input("Underlying", placeholder="AAPL").strip().upper()
        option_type = c2.selectbox("Type", ["PUT", "CALL"])
        open_date = c3.date_input("Open Date")

        c4, c5, c6 = st.columns(3)
        expiry = c4.date_input("Expiry")
        strike = c5.number_input("Strike", min_value=0.0, step=0.5, format="%.2f")
        quantity = c6.number_input("Contracts", min_value=1, value=1)

        c7, c8, c9 = st.columns(3)
        premium = c7.number_input("Premium/Contract", min_value=0.0, step=0.05, format="%.2f")
        fees = c8.number_input("Fees", value=0.0, step=0.01, format="%.2f")
        delta = c9.number_input("Delta", value=0.0, step=0.01, format="%.2f")

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
                    "CloseDate": None,
                }
                orders = pd.concat([orders, pd.DataFrame([new_order])], ignore_index=True)
                save_table(orders, "orders.csv")
                st.success(f"Order {new_id} added successfully.")
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
            view_data = view_data.sort_values(["OpenDate", "ID"]).reset_index(drop=True)
            view_data["Notes"] = view_data["Notes"].fillna("").astype(str)
            view_data["Underlying"] = view_data["Underlying"].astype(str)
            view_data["Side"] = view_data["Side"].astype(str)
            view_data["Type"] = view_data["Type"].astype(str)
            view_data["Status"] = view_data["Status"].astype(str)

            edited_data = st.data_editor(
                view_data,
                width="stretch",
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
                },
            )

            if st.button("Save Changes"):
                edited_data["Underlying"] = edited_data["Underlying"].astype(str).map(clean_ticker)
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
                st.success("Orders updated successfully.")
                st.rerun()

with tab6:
    render_analytics()

# Footer
st.markdown("---")
st.caption("Option Wheel Strategy Dashboard - Modular Version")
