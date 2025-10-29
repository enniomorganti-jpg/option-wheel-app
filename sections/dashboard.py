# sections/dashboard.py
import streamlit as st
import pandas as pd
import altair as alt

import config
from database import load_table, save_table
from analytics import (
    rebuild_realized_from_orders,
    build_cash_ledger_inventory_aware,
    cash_timeline_df,
)
from utils import positions_nonzero, clean_ticker, format_currency
from pricing import (
    get_price_for,
    set_price_for,
    refresh_all_prices_yf,   # yfinance
)

# ------------------------------------------------------------
# Helper: collateral su PUT aperte (valore istantaneo)
# ------------------------------------------------------------
def calculate_csp_collateral(orders_df: pd.DataFrame) -> float:
    """
    Collateral = somma Strike * 100 * Qty per tutte le PUT ancora OPEN.
    """
    if orders_df is None or orders_df.empty:
        return 0.0
    o = orders_df.copy()
    o["Type"] = o.get("Type", "").astype(str).str.upper()
    o["Status"] = o.get("Status", "").astype(str).str.upper()
    o["Strike"] = pd.to_numeric(o.get("Strike", 0), errors="coerce").fillna(0.0)
    o["Qty"] = pd.to_numeric(o.get("Qty", 0), errors="coerce").fillna(0).astype(int)

    open_puts = o[(o["Type"] == "PUT") & (o["Status"] == "OPEN")]
    if open_puts.empty:
        return 0.0

    collateral = (open_puts["Strike"] * 100.0 * open_puts["Qty"]).sum()
    return float(collateral or 0.0)


# ------------------------------------------------------------
# Helper: timeline del collateral (cumulativo nel tempo)
# Regole:
#  - all'OPEN di una PUT (side Sell) aggiungo +strike*100*qty
#  - alla CLOSE (status != OPEN) rimuovo -strike*100*qty
#  - Expired/Assigned/Closed liberano il vincolo alla CloseDate
# ------------------------------------------------------------
def build_csp_collateral_timeline(orders_df: pd.DataFrame) -> pd.DataFrame:
    cols = ["Date", "DeltaCollateral"]
    if orders_df is None or orders_df.empty:
        return pd.DataFrame(columns=cols + ["CollateralOutstanding"])

    o = orders_df.copy()
    # normalizza tipi e numeri
    for c in ("OpenDate", "CloseDate"):
        if c in o.columns:
            o[c] = pd.to_datetime(o[c], errors="coerce")
    o["Type"] = o.get("Type", "").astype(str).str.upper()
    o["Side"] = o.get("Side", "Sell").astype(str).str.title()
    o["Status"] = o.get("Status", "").astype(str).str.upper()
    o["Strike"] = pd.to_numeric(o.get("Strike", 0), errors="coerce").fillna(0.0)
    o["Qty"] = pd.to_numeric(o.get("Qty", 0), errors="coerce").fillna(0).astype(int)

    events = []

    # Aggiungo evento all'apertura (vincolo entra)
    mask_open_put_sell = (
        (o["Type"] == "PUT") &
        (o["Side"] == "Sell") &
        (o["Qty"] > 0) &
        o["OpenDate"].notna()
    )
    if mask_open_put_sell.any():
        add_df = o.loc[mask_open_put_sell, ["OpenDate", "Strike", "Qty"]].copy()
        add_df["Date"] = add_df["OpenDate"].dt.date
        add_df["DeltaCollateral"] = (add_df["Strike"] * 100.0 * add_df["Qty"]).astype(float)
        events.append(add_df[["Date", "DeltaCollateral"]])

    # Alla chiusura (qualsiasi status != OPEN) rimuovo vincolo
    mask_close_put = (
        (o["Type"] == "PUT") &
        (o["Qty"] > 0) &
        (o["Status"] != "OPEN") &
        o["CloseDate"].notna()
    )
    if mask_close_put.any():
        rel_df = o.loc[mask_close_put, ["CloseDate", "Strike", "Qty"]].copy()
        rel_df["Date"] = rel_df["CloseDate"].dt.date
        rel_df["DeltaCollateral"] = -(rel_df["Strike"] * 100.0 * rel_df["Qty"]).astype(float)
        events.append(rel_df[["Date", "DeltaCollateral"]])

    if not events:
        return pd.DataFrame(columns=cols + ["CollateralOutstanding"])

    ev = pd.concat(events, ignore_index=True)
    ev = (ev.groupby("Date", as_index=False)["DeltaCollateral"]
            .sum()
            .sort_values("Date")
            .reset_index(drop=True))
    ev["CollateralOutstanding"] = ev["DeltaCollateral"].cumsum()
    # garantisco non negatività (robusto se input incoerente)
    ev["CollateralOutstanding"] = ev["CollateralOutstanding"].clip(lower=0.0)
    return ev[["Date", "DeltaCollateral", "CollateralOutstanding"]]


# ------------------------------------------------------------
# RENDER
# ------------------------------------------------------------
def render_dashboard(settings: dict):

    # --- Load base tables
    orders = load_table(config.ORDERS_CSV, [])
    positions = load_table(config.POSITIONS_CSV, [])
    realized = load_table(config.REALIZED_CSV, [])

    # --- Market Value (azioni)
    p = positions_nonzero(positions)
    if p.empty:
        market_value = 0.0
        comp = pd.DataFrame(columns=["Underlying", "Qty", "AvgCost", "Last", "MarketValue"])
    else:
        comp = p.copy()
        comp["Underlying"] = comp["Underlying"].astype(str).map(clean_ticker)
        comp["Qty"] = pd.to_numeric(comp["Qty"], errors="coerce").fillna(0).astype(int)
        comp["AvgCost"] = pd.to_numeric(comp["AvgCost"], errors="coerce").fillna(0.0)
        comp["Last"] = comp["Underlying"].apply(lambda u: get_price_for(u) or 0.0)
        comp["MarketValue"] = (comp["Qty"] * comp["Last"]).astype(float)
        market_value = float(comp["MarketValue"].sum())

    # --- Cashflows: ledger -> timeline (qui entrano i premi!)
    start_cash = float(settings.get("starting_cash", config.DEFAULT_STARTING_CASH))
    ledger, audit = build_cash_ledger_inventory_aware(orders, positions)
    timeline = cash_timeline_df(ledger, start_cash)
    current_cash = float(timeline["CumulativeCash"].iloc[-1]) if not timeline.empty else start_cash

    # --- Collateral attivo su PUT aperte (istantaneo)
    csp_collateral_now = calculate_csp_collateral(orders)

    # --- Timeline collateral (per Unused Cash)
    coll_tl = build_csp_collateral_timeline(orders)

    # --- Merge: FreeCash = Cash (ledger) - Collateral
    if not timeline.empty or not coll_tl.empty:
        free_tl = (
            pd.merge(
                timeline[["Date", "CumulativeCash"]] if not timeline.empty else pd.DataFrame({"Date": [], "CumulativeCash": []}),
                coll_tl[["Date", "CollateralOutstanding"]] if not coll_tl.empty else pd.DataFrame({"Date": [], "CollateralOutstanding": []}),
                on="Date",
                how="outer",
            )
            .sort_values("Date")
            .reset_index(drop=True)
        )
        free_tl["CumulativeCash"] = free_tl["CumulativeCash"].ffill().fillna(start_cash)
        free_tl["CollateralOutstanding"] = free_tl["CollateralOutstanding"].ffill().fillna(0.0)
        free_tl["FreeCash"] = free_tl["CumulativeCash"] - free_tl["CollateralOutstanding"]
    else:
        free_tl = pd.DataFrame(columns=["Date", "CumulativeCash", "CollateralOutstanding", "FreeCash"])

    # --- Realized (ricostruito)
    _real = rebuild_realized_from_orders(orders)
    realized_total = float(_real["TotalPL"].sum()) if not _real.empty else 0.0

    # --- Total premiums collected (lifetime)
    sold = orders.copy()
    if not sold.empty:
        sold["Side"] = sold["Side"].astype(str).str.upper()
        sold["PricePerContract"] = pd.to_numeric(sold["PricePerContract"], errors="coerce").fillna(0.0)
        sold["Qty"] = pd.to_numeric(sold["Qty"], errors="coerce").fillna(0).astype(int)
        total_premiums = float(
            (sold[sold["Side"] == "SELL"]["PricePerContract"] * 100.0 * sold[sold["Side"] == "SELL"]["Qty"]).sum()
        )
    else:
        total_premiums = 0.0

    # --- Portfolio value (no doppio conteggio del collateral)
    # Portafoglio = Cash (ledger) + Market Value azioni
    portfolio_value_now = current_cash + market_value

    # ========================================================
    # TOP: Total Portfolio Value (grande) + metriche richieste
    # ========================================================
    st.markdown(
        f"""
        <div class="tpv-wrap">
        <div class="tpv-title">Total portfolio value</div>
        <div class="tpv-value">{format_currency(portfolio_value_now)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Total premiums collected", format_currency(total_premiums))
    with c2:
        st.metric("Realized gain/loss", format_currency(realized_total))
    with c3:
        st.metric("Cash balance", format_currency(current_cash))
    with c4:
        st.metric("Mkt value positions", format_currency(market_value))
    with c5:
        st.metric("CSP collateral", format_currency(csp_collateral_now))

    # =========================
    # Cashflows + grafico
    # =========================
    st.markdown("---")
    st.subheader("Cashflows")

    last_free = float(free_tl["FreeCash"].iloc[-1]) if not free_tl.empty else current_cash
    if not free_tl.empty:
        chart = (
            alt.Chart(free_tl)
            .mark_line(point=True)
            .encode(
                x=alt.X("Date:T", title="Date"),
                y=alt.Y("FreeCash:Q", title="Free Cash"),
                tooltip=[
                    alt.Tooltip("Date:T", title="Date"),
                    alt.Tooltip("FreeCash:Q", title="Free Cash", format=",.2f"),
                    alt.Tooltip("CumulativeCash:Q", title="Cash gross", format=",.2f"),
                    alt.Tooltip("CollateralOutstanding:Q", title="Collateral", format=",.2f"),
                ],
            )
            .properties(height=280)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.caption("Nessun evento di cassa ancora — uso lo starting cash.")

    # =========================
    # Dettagli: timeline cash + ledger + audit
    # =========================
    d1, d2, d3 = st.columns(3)
    with d1:
        st.write("**Cash over time**")
        st.dataframe(free_tl, width="stretch", height=260)
    with d2:
        st.write("**Event tracker**")
        st.dataframe(ledger, width="stretch", height=260)
    with d3:
        st.write("**Shares Balance**")
        st.dataframe(audit, width="stretch", height=260)

    # =========================
    # Market prices (yfinance only)
    # =========================
    st.markdown("---")
    st.subheader("Market Prices")

    all_ul = (
        sorted(
            positions_nonzero(positions)["Underlying"]
            .astype(str)
            .map(clean_ticker)
            .unique()
            .tolist()
        )
        if not positions.empty and "Underlying" in positions.columns
        else []
    )

    if not all_ul:
        st.caption("No ticker in position.")
    else:
        if st.button("Update prices (yfinance)"):
            with st.spinner("Updating prices with Yahoo..."):
                fetched, errs = refresh_all_prices_yf(all_ul)
                for ul, px in fetched.items():
                    set_price_for(ul, px)
            if fetched:
                st.success(f"Updated {len(fetched)} ticker (Yahoo).")
                st.rerun()

        with st.expander("Current Prices", expanded=False):
            price_rows = [{"Ticker": ul, "Current Price": format_currency(get_price_for(ul) or 0.0)} for ul in all_ul]
            st.dataframe(pd.DataFrame(price_rows), width="stretch")
