# sections/dashboard.py
import streamlit as st
import pandas as pd
import altair as alt
from database import load_table, save_table
from analytics import rebuild_realized_from_orders, build_cash_ledger_inventory_aware, cash_timeline_df
from utils import positions_nonzero, clean_ticker, format_currency
from pricing import get_price_for, set_price_for, refresh_all_prices_yf, refresh_prices_ibkr, IB_AVAILABLE
import config

def calculate_csp_collateral(orders_df):
    if orders_df.empty:
        return 0.0
    
    open_puts = orders_df[
        (orders_df["Type"].str.upper() == "PUT") & 
        (orders_df["Status"].str.upper() == "OPEN")
    ]
    
    collateral = 0.0
    for _, put in open_puts.iterrows():
        strike = float(put.get("Strike", 0))
        qty = int(put.get("Qty", 0))
        collateral += strike * 100 * qty
    
    return collateral

def calculate_real_cash_balance(settings, orders_df, realized_df, market_value):
    starting_cash = float(settings.get("starting_cash", 0.0))
    
    sold_options = orders_df[orders_df['Side'].str.upper() == 'SELL']
    total_premiums = (sold_options['PricePerContract'] * 100 * sold_options['Qty']).sum()
    
    realized_gains = realized_df['TotalPL'].sum() if not realized_df.empty and 'TotalPL' in realized_df.columns else 0
    
    csp_collateral = calculate_csp_collateral(orders_df)
    
    total_portfolio_value = starting_cash + total_premiums + realized_gains
    free_cash = total_portfolio_value - market_value - csp_collateral
    
    return free_cash, total_premiums, csp_collateral, total_portfolio_value

def render_dashboard(settings):
    st.subheader("Dashboard")
    
    orders = load_table(config.ORDERS_CSV, [])
    positions = load_table(config.POSITIONS_CSV, [])
    realized = load_table(config.REALIZED_CSV, [])
    
    # Calcola market value
    p = positions_nonzero(positions)
    if p.empty:
        market_value = 0.0
        comp = pd.DataFrame(columns=["Underlying","Qty","AvgCost","Last","MarketValue"])
    else:
        comp = p.copy()
        comp["Underlying"] = comp["Underlying"].astype(str).map(clean_ticker)
        comp["Last"] = comp["Underlying"].apply(lambda u: get_price_for(u) or 0.0)
        comp["MarketValue"] = (comp["Qty"] * comp["Last"]).astype(float)
        market_value = float(comp["MarketValue"].sum())
    
    # Calcola cash reale
    real_cash_balance, total_premiums, csp_collateral, total_portfolio_value = calculate_real_cash_balance(settings, orders, realized, market_value)
    
    with st.expander("Maintenance"):
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("Rebuild realized from orders"):
                _real = rebuild_realized_from_orders(orders)
                save_table(_real, config.REALIZED_CSV)
                st.success("Realized ricostruito e salvato da orders.")
                st.rerun()
        
        with col2:
            if st.button("💀 RESET DATABASE", type="secondary"):
                empty_orders = pd.DataFrame(columns=[
                    "ID", "Underlying", "Side", "Type", "OpenDate", "Expiry", 
                    "Strike", "Qty", "PricePerContract", "Fees", "Delta", 
                    "Notes", "Status", "CloseDate"
                ])
                empty_positions = pd.DataFrame(columns=["Underlying", "Qty", "AvgCost"])
                empty_realized = pd.DataFrame(columns=[
                    "Underlying", "Event", "EventDate", "Shares", "Strike", 
                    "PremiumPerShare", "AvgCostAtEvent", "EquityPL", "TotalPL", "Notes"
                ])
                
                save_table(empty_orders, config.ORDERS_CSV)
                save_table(empty_positions, config.POSITIONS_CSV)
                save_table(empty_realized, config.REALIZED_CSV)
                
                st.error("🗑️ DATABASE RESETTATO!")
                st.rerun()

    st.markdown("---")
    st.subheader("Cashflows")

    ledger, audit = build_cash_ledger_inventory_aware(orders, positions)
    start_cash = float(settings.get("starting_cash", 0.0))
    timeline = cash_timeline_df(ledger, start_cash)
    
    colL, colR = st.columns([1, 1])
    
    with colL:
        with st.expander("Cash ledger (inventory-aware)", expanded=False):
            display_ledger = ledger.copy()
            if "CashFlow" in display_ledger.columns:
                display_ledger["CashFlow"] = display_ledger["CashFlow"].apply(format_currency)
            
            st.dataframe(display_ledger, use_container_width=True)

        with st.expander("Audit shares balance", expanded=False):
            display_audit = audit.copy()
            if "CashFlow" in display_audit.columns:
                display_audit["CashFlow"] = display_audit["CashFlow"].apply(format_currency)
            st.dataframe(display_audit, use_container_width=True)

    with colR:
        st.caption("Cash Balance Reale - Con Gestione Collateral")
        
        # Usa la timeline calcolata dalla nuova logica di analytics.py
        if not timeline.empty:
            # Calcola l'ultimo valore del cash balance
            last_cash = timeline['CumulativeCash'].iloc[-1] if not timeline.empty else start_cash
            
            st.metric("Cash Balance Attuale", format_currency(last_cash), 
                     delta=format_currency(last_cash - start_cash))
            
            # Crea il grafico con la timeline corretta
            chart = (
                alt.Chart(timeline)
                .mark_line(point=True, color='green')
                .encode(
                    x=alt.X("Date:T", title="Data"),
                    y=alt.Y("CumulativeCash:Q", title="Cash Balance ($)"),
                    tooltip=[
                        alt.Tooltip("Date:T", title="Data"),
                        alt.Tooltip("DailyFlow:Q", title="Daily Flow", format=",.2f"),
                        alt.Tooltip("CumulativeCash:Q", title="Cash Balance", format=",.2f"),
                    ],
                )
                .properties(height=300)
            )
            
            # Aggiungi linea dello zero per riferimento
            zero_line = alt.Chart(pd.DataFrame({'y': [0]})).mark_rule(strokeDash=[5,5], color='red').encode(y='y:Q')
            
            st.altair_chart(chart + zero_line, use_container_width=True)
            
            # Mostra dettagli della timeline
            with st.expander("Dettagli Timeline Cash", expanded=False):
                display_timeline = timeline.copy()
                display_timeline["DailyFlow"] = display_timeline["DailyFlow"].apply(format_currency)
                display_timeline["CumulativeCash"] = display_timeline["CumulativeCash"].apply(format_currency)
                st.dataframe(display_timeline, use_container_width=True)
        else:
            st.metric("Cash Balance", format_currency(start_cash))
            st.caption("No cash flow events yet - Using starting cash")

    # Overview metrics - Ricalcolate per coerenza
    _real = rebuild_realized_from_orders(orders)
    realized_total = float(_real["TotalPL"].sum()) if not _real.empty else 0.0
    
    # Usa il cash balance dalla timeline invece del calcolo vecchio
    current_cash_balance = timeline['CumulativeCash'].iloc[-1] if not timeline.empty else start_cash
    portfolio_value_now = current_cash_balance + market_value + csp_collateral

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cash Balance", format_currency(current_cash_balance))
    c2.metric("Mkt. value positions", format_currency(market_value))
    c3.metric("CSP Collateral", format_currency(csp_collateral))
    c4.metric("Total Portfolio Value", format_currency(portfolio_value_now))

    # Verifica della coerenza
    st.info(f"**Verifica Portafoglio:** {format_currency(current_cash_balance)} (Cash) + {format_currency(market_value)} (Positions) + {format_currency(csp_collateral)} (Collateral) = {format_currency(portfolio_value_now)}")

    # Realized P/L separato
    r1, r2 = st.columns(2)
    with r1:
        st.metric("Realized Gain/Loss", format_currency(realized_total),
                 delta=format_currency(realized_total))
    with r2:
        total_premiums_collected = (orders[orders['Side'].str.upper() == 'SELL']['PricePerContract'] * 100 * orders[orders['Side'].str.upper() == 'SELL']['Qty']).sum()
        st.metric("Total Premiums Collected", format_currency(total_premiums_collected))

    # Positions
    st.markdown("---")
    st.subheader("Positions")
    if p.empty:
        st.info("No positions.")
    else:
        display_comp = comp.copy()
        display_comp["AvgCost"] = display_comp["AvgCost"].apply(format_currency)
        display_comp["Last"] = display_comp["Last"].apply(format_currency)
        display_comp["MarketValue"] = display_comp["MarketValue"].apply(format_currency)
        
        st.dataframe(
            display_comp[["Underlying", "Qty", "AvgCost", "Last", "MarketValue"]]
              .sort_values("MarketValue", ascending=False)
              .rename(columns={
                  "Underlying":"Ticker","Qty":"Shares","AvgCost":"Avg Cost",
                  "Last":"Last","MarketValue":"Value"
              }),
            use_container_width=True,
        )

    # Market prices section
    st.markdown("---")
    st.subheader("Market Prices")

    all_ul = (
        sorted(positions_nonzero(positions)["Underlying"].astype(str).map(clean_ticker).unique().tolist())
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

        # Mostra i prezzi correnti
        with st.expander("Current Prices", expanded=False):
            price_data = []
            for ul in all_ul:
                current_price = get_price_for(ul) or 0.0
                price_data.append({
                    "Ticker": ul,
                    "Current Price": format_currency(current_price)
                })
            if price_data:
                st.dataframe(pd.DataFrame(price_data), use_container_width=True)

    # Debug Realized Gain
    with st.expander("Debug Realized Gain", expanded=False):
        st.write("Realized calcolato:", format_currency(realized_total))
        if not _real.empty:
            st.dataframe(_real)

    # Debug Orders Status
    with st.expander("Debug Orders Status", expanded=False):
        if not orders.empty:
            status_counts = orders['Status'].value_counts()
            st.write("Orders by status:")
            for status, count in status_counts.items():
                st.write(f"- {status}: {count}")
            
            # Mostra PUT aperti con collateral
            open_puts = orders[
                (orders['Type'].str.upper() == 'PUT') & 
                (orders['Status'].str.upper() == 'OPEN')
            ]
            if not open_puts.empty:
                st.write("Open PUTs (active collateral):")
                st.dataframe(open_puts[['ID', 'Underlying', 'Strike', 'Qty', 'Expiry']], use_container_width=True)