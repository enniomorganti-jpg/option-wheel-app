# sections/cashflows.py
import streamlit as st
import pandas as pd
from database import load_table
from analytics import build_cash_ledger_inventory_aware, cash_timeline_df
from utils import format_currency

def render_cashflows(settings):
    st.subheader("Cash Flow Analysis")
    
    orders = load_table("orders.csv", [])
    positions = load_table("positions.csv", [])
    realized = load_table("realized.csv", [])
    
    starting_cash = float(settings.get("starting_cash", 0.0))
    
    # CALCOLO PORTAFOGLIO TOTALE (come da tua logica)
    # 1. Valore attuale delle shares
    positions_value = 0
    if not positions.empty:
        # Qui dovremmo avere i prezzi correnti, per ora uso una stima
        # In realtà questo dovrebbe venire dal pricing.py
        positions_value = positions.get('CurrentValue', 0).sum() if 'CurrentValue' in positions.columns else 0
    
    # 2. Premium totali da tutte le opzioni vendute
    sold_options = orders[orders['Side'].str.upper() == 'SELL']
    total_premiums = (sold_options['PricePerContract'] * 100 * sold_options['Qty']).sum()
    
    # 3. CSP Collateral per PUT aperti
    open_puts = orders[
        (orders['Type'].str.upper() == 'PUT') & 
        (orders['Status'].str.upper() == 'OPEN')
    ]
    csp_collateral = (open_puts['Strike'] * 100 * open_puts['Qty']).sum()
    
    # 4. Realized gains
    realized_gains = realized['TotalPL'].sum() if not realized.empty and 'TotalPL' in realized.columns else 0
    
    # CALCOLO FINALE SECONDO LA TUA LOGICA
    total_portfolio_value = starting_cash + total_premiums + realized_gains
    cash_ledger_balance = total_portfolio_value - positions_value - csp_collateral

    # Visualizzazione
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.metric("Starting Cash", format_currency(starting_cash))
        st.metric("Total Premiums Collected", format_currency(total_premiums))
        st.metric("Realized Gains", format_currency(realized_gains))
        st.metric("Total Portfolio Value", format_currency(total_portfolio_value))
    
    with col2:
        st.metric("Market Value Positions", format_currency(positions_value))
        st.metric("CSP Collateral", format_currency(csp_collateral))
        st.metric("Cash Ledger (libero)", format_currency(cash_ledger_balance), 
                 delta=format_currency(cash_ledger_balance) if cash_ledger_balance != 0 else None)

    # VERIFICA: Dovrebbe essere circa -3k
    st.info(f"**Verifica:** {format_currency(total_portfolio_value)} - {format_currency(positions_value)} - {format_currency(csp_collateral)} = {format_currency(cash_ledger_balance)}")

    # DETTAGLI COLLATERAL
    with st.expander("CSP Collateral Details", expanded=False):
        if open_puts.empty:
            st.caption("No open PUT positions.")
        else:
            collateral_detail = open_puts.copy()
            collateral_detail['Collateral'] = collateral_detail['Strike'] * 100 * collateral_detail['Qty']
            collateral_detail = collateral_detail[['Underlying', 'Strike', 'Qty', 'Collateral', 'Expiry']]
            collateral_detail['Collateral'] = collateral_detail['Collateral'].apply(format_currency)
            st.dataframe(collateral_detail, use_container_width=True, hide_index=True)

    # LEDGER STORICO (solo per riferimento visivo)
    ledger, audit = build_cash_ledger_inventory_aware(orders, positions)
    with st.expander("Historical Cash Ledger (solo premium)", expanded=False):
        if ledger.empty:
            st.caption("No cash flow events recorded.")
        else:
            display_ledger = ledger.copy()
            if "CashFlow" in display_ledger.columns:
                display_ledger["CashFlow"] = display_ledger["CashFlow"].apply(format_currency)
            st.dataframe(display_ledger, use_container_width=True)

    # CASH TIMELINE (solo per riferimento visivo)
    timeline = cash_timeline_df(ledger, starting_cash)
    with st.expander("Cash Timeline Chart (solo premium)", expanded=False):
        if not timeline.empty:
            import altair as alt
            chart = (
                alt.Chart(timeline)
                .mark_line(point=True)
                .encode(
                    x=alt.X("Date:T", title="Date"),
                    y=alt.Y("CumulativeCash:Q", title="Cash Balance ($)"),
                    tooltip=[
                        alt.Tooltip("Date:T", title="Date"),
                        alt.Tooltip("DailyFlow:Q", title="Daily Flow", format=",.2f"),
                        alt.Tooltip("CumulativeCash:Q", title="Cash Balance", format=",.2f"),
                    ],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption("No cash flow data to display.")