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
    refresh_all_prices_yf,
    refresh_prices_ibkr,
    IB_AVAILABLE,
)

# ------------------------------------------------------------
# Helper: collateral su PUT aperte (valore istantaneo)
# ------------------------------------------------------------
def calculate_csp_collateral(orders_df: pd.DataFrame) -> float:
    """Collateral = somma Strike * 100 * Qty per tutte le PUT ancora OPEN."""
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
# Helper: timeline collateral (cumulativo)
#  - OPEN PUT (Sell): + strike*100*qty
#  - Close (status != OPEN): - strike*100*qty
# ------------------------------------------------------------
def build_csp_collateral_timeline(orders_df: pd.DataFrame) -> pd.DataFrame:
    cols = ["Date", "DeltaCollateral"]
    if orders_df is None or orders_df.empty:
        return pd.DataFrame(columns=cols + ["CollateralOutstanding"])

    o = orders_df.copy()
    for c in ("OpenDate", "CloseDate"):
        if c in o.columns:
            o[c] = pd.to_datetime(o[c], errors="coerce")
    o["Type"] = o.get("Type", "").astype(str).str.upper()
    o["Side"] = o.get("Side", "Sell").astype(str).str.title()
    o["Status"] = o.get("Status", "").astype(str).str.upper()
    o["Strike"] = pd.to_numeric(o.get("Strike", 0), errors="coerce").fillna(0.0)
    o["Qty"] = pd.to_numeric(o.get("Qty", 0), errors="coerce").fillna(0).astype(int)

    events = []

    # Ingresso collateral
    mask_open = (o["Type"] == "PUT") & (o["Side"] == "Sell") & (o["Qty"] > 0) & o["OpenDate"].notna()
    if mask_open.any():
        add_df = o.loc[mask_open, ["OpenDate", "Strike", "Qty"]].copy()
        add_df["Date"] = add_df["OpenDate"].dt.date
        add_df["DeltaCollateral"] = (add_df["Strike"] * 100.0 * add_df["Qty"]).astype(float)
        events.append(add_df[["Date", "DeltaCollateral"]])

    # Rilascio collateral
    mask_close = (o["Type"] == "PUT") & (o["Qty"] > 0) & (o["Status"] != "OPEN") & o["CloseDate"].notna()
    if mask_close.any():
        rel_df = o.loc[mask_close, ["CloseDate", "Strike", "Qty"]].copy()
        rel_df["Date"] = rel_df["CloseDate"].dt.date
        rel_df["DeltaCollateral"] = -(rel_df["Strike"] * 100.0 * rel_df["Qty"]).astype(float)
        events.append(rel_df[["Date", "DeltaCollateral"]])

    if not events:
        return pd.DataFrame(columns=cols + ["CollateralOutstanding"])

    ev = pd.concat(events, ignore_index=True)
    ev = (
        ev.groupby("Date", as_index=False)["DeltaCollateral"]
        .sum()
        .sort_values("Date")
        .reset_index(drop=True)
    )
    ev["CollateralOutstanding"] = ev["DeltaCollateral"].cumsum().clip(lower=0.0)
    return ev[["Date", "DeltaCollateral", "CollateralOutstanding"]]

# ------------------------------------------------------------
# RENDER
# ------------------------------------------------------------
def render_dashboard(settings: dict):
    st.subheader("Dashboard")

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

    # --- Cashflows: ledger -> timeline
    start_cash = float(settings.get("starting_cash", config.DEFAULT_STARTING_CASH))
    ledger, audit = build_cash_ledger_inventory_aware(orders, positions)
    timeline = cash_timeline_df(ledger, start_cash)
    current_cash = float(timeline["CumulativeCash"].iloc[-1]) if not timeline.empty else start_cash

    # --- Collateral istantaneo + timeline collateral
    csp_collateral_now = calculate_csp_collateral(orders)
    coll_tl = build_csp_collateral_timeline(orders)

    # --- Merge cash + collateral -> FreeCash timeline
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

    last_free = float(free_tl["FreeCash"].iloc[-1]) if not free_tl.empty else current_cash

    # --- Realized (ricostruito) + Total Premiums
    _real = rebuild_realized_from_orders(orders)
    realized_total = float(_real["TotalPL"].sum()) if not _real.empty else 0.0

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
    portfolio_value_now = current_cash + market_value

    # =========================
    # Maintenance
    # =========================
    with st.expander("Maintenance"):
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Rebuild realized from orders"):
                _real_b = rebuild_realized_from_orders(orders)
                save_table(_real_b, config.REALIZED_CSV)
                st.success("Realized ricostruito e salvato da orders.")
                st.rerun()
        with col2:
            if st.button("RESET DATABASE", type="secondary"):
                empty_orders = pd.DataFrame(
                    columns=[
                        "ID","Underlying","Side","Type","OpenDate","Expiry","Strike",
                        "Qty","PricePerContract","Fees","Delta","Notes","Status","CloseDate"
                    ]
                )
                empty_positions = pd.DataFrame(columns=["Underlying", "Qty", "AvgCost"])
                empty_realized = pd.DataFrame(
                    columns=[
                        "Underlying","Event","EventDate","Shares","Strike",
                        "PremiumPerShare","AvgCostAtEvent","EquityPL","TotalPL","Notes"
                    ]
                )
                save_table(empty_orders, config.ORDERS_CSV)
                save_table(empty_positions, config.POSITIONS_CSV)
                save_table(empty_realized, config.REALIZED_CSV)
                st.error("Database resettato.")
                st.rerun()

    # ======== TOP: PORTFOLIO VALUE ========
    st.markdown(
        f"""
        <div class="tpv-wrap">
            <div class="tpv-title">Total Portfolio Value</div>
            <div class="tpv-value">{format_currency(portfolio_value_now)}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # Prima riga: Mkt value  |  CSP Collateral
    c1, c2 = st.columns(2)
    with c1:
        st.metric("Mkt. value positions", format_currency(market_value))
    with c2:
        st.metric("CSP Collateral (vincolo)", format_currency(csp_collateral_now))

    # Seconda riga: Premiums | Realized
    r1, r2 = st.columns(2)
    with r1:
        st.metric("Total Premiums Collected (lifetime)", format_currency(total_premiums))
    with r2:
        st.metric("Realized Gain/Loss (rebuilt)", format_currency(realized_total))

    st.markdown("---")

    # ======== CASHFLOWS (grafico e dettagli) ========
    st.subheader("Cashflows")

    st.caption("Unused cash (Cash − Collateral PUT)")
    st.metric("Cash disponibile", format_currency(last_free))

    if not free_tl.empty:
        chart = (
            alt.Chart(free_tl)
            .mark_line(point=True)
            .encode(
                x=alt.X("Date:T", title="Data"),
                y=alt.Y("FreeCash:Q", title="Unused Cash"),
                tooltip=[
                    alt.Tooltip("Date:T", title="Data"),
                    alt.Tooltip("FreeCash:Q", title="Free Cash", format=",.2f"),
                    alt.Tooltip("CumulativeCash:Q", title="Cash lordo", format=",.2f"),
                    alt.Tooltip("CollateralOutstanding:Q", title="Collateral", format=",.2f"),
                ],
            )
            .properties(height=320)
        )
        zero_line = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeDash=[5, 5], color="red").encode(y="y:Q")
        st.altair_chart(chart + zero_line)

        with st.expander("Dettagli Timeline Cash", expanded=False):
            display_tl = free_tl.copy()
            display_tl["FreeCash"] = display_tl["FreeCash"].apply(format_currency)
            display_tl["CumulativeCash"] = display_tl["CumulativeCash"].apply(format_currency)
            display_tl["CollateralOutstanding"] = display_tl["CollateralOutstanding"].apply(format_currency)
            st.dataframe(display_tl, width="stretch")
    else:
        st.caption("Nessun evento di cassa ancora — uso lo starting cash.")

    st.markdown("")

    # ======== Ledger & Audit ========
    colL, colR = st.columns(2)
    with colL:
        with st.expander("Cash ledger (inventory-aware)", expanded=False):
            display_ledger = ledger.copy()
            if not display_ledger.empty and "CashFlow" in display_ledger.columns:
                display_ledger["CashFlow"] = display_ledger["CashFlow"].apply(format_currency)
            st.dataframe(display_ledger, width="stretch")
    with colR:
        with st.expander("Audit shares balance", expanded=False):
            st.dataframe(audit, width="stretch")

    # ======== Positions ========
    st.markdown("---")
    st.subheader("Positions")
    if p.empty:
        st.info("No active positions.")
    else:
        display_comp = comp.copy()
        display_comp["AvgCost"] = display_comp["AvgCost"].apply(format_currency)
        display_comp["Last"] = display_comp["Last"].apply(format_currency)
        display_comp["MarketValue"] = display_comp["MarketValue"].apply(format_currency)

        st.dataframe(
            display_comp[["Underlying", "Qty", "AvgCost", "Last", "MarketValue"]]
            .sort_values("MarketValue", ascending=False)
            .rename(
                columns={
                    "Underlying": "Ticker",
                    "Qty": "Shares",
                    "AvgCost": "Avg Cost",
                    "Last": "Last",
                    "MarketValue": "Value",
                }
            ),
            width="stretch",
        )

    # ======== Market prices ========
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
        colA, colB = st.columns(2)

        with colA:
            if st.button("Update prices (yfinance)"):
                with st.spinner("Updating prices with Yahoo..."):
                    fetched, errs = refresh_all_prices_yf(all_ul)
                    for ul, px in fetched.items():
                        set_price_for(ul, px)
                if fetched:
                    st.success(f"Updated {len(fetched)} ticker (Yahoo).")
                    st.rerun()

        with colB:
            if st.button("Update prices (IBKR)"):
                if not IB_AVAILABLE:
                    st.error("ib_insync not installed.")
                else:
                    with st.spinner("Connection to IBKR..."):
                        fetched, errs = refresh_prices_ibkr(all_ul)
                        for ul, px in fetched.items():
                            set_price_for(ul, px)
                    if fetched:
                        st.success(f"Updated {len(fetched)} ticker (IBKR).")
                        st.rerun()

        with st.expander("Current Prices", expanded=False):
            price_rows = [{"Ticker": ul, "Current Price": format_currency(get_price_for(ul) or 0.0)} for ul in all_ul]
            st.dataframe(pd.DataFrame(price_rows), width="stretch")

    # ======== Debug ========
    with st.expander("Debug Realized (rebuilt)", expanded=False):
        if _real.empty:
            st.caption("Nessun realized generato.")
        else:
            st.dataframe(_real, width="stretch")

    with st.expander("Debug Orders Status", expanded=False):
        if not orders.empty:
            status_counts = orders["Status"].value_counts(dropna=False)
            st.write("Orders by status:")
            for status, count in status_counts.items():
                st.write(f"- {status}: {count}")

            open_puts = orders[
                (orders["Type"].astype(str).str.upper() == "PUT")
                & (orders["Status"].astype(str).str.upper() == "OPEN")
            ][["ID", "Underlying", "Strike", "Qty", "Expiry"]]
            if not open_puts.empty:
                st.write("Open PUTs (active collateral):")
                st.dataframe(open_puts, width="stretch")