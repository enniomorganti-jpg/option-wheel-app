# sections/analytics.py
import streamlit as st
import pandas as pd
import altair as alt
from database import load_table
from analytics import rebuild_realized_from_orders
from utils import clean_ticker, positions_nonzero
from pricing import get_price_for

def render_analytics():
    st.subheader("Analytics")
    
    orders = load_table("orders.csv", [])
    positions = load_table("positions.csv", [])
    
    # Portfolio Composition
    st.markdown("**Portfolio Composition (Market Value)**")
    if positions.empty:
        st.caption("No positions.")
    else:
        comp = positions_nonzero(positions)
        comp["Underlying"] = comp["Underlying"].astype(str).map(clean_ticker)
        comp["Last"] = comp["Underlying"].apply(lambda u: get_price_for(u) or 0.0)
        comp["MarketValue"] = (comp["Qty"] * comp["Last"]).astype(float)
        comp = comp[comp["MarketValue"] > 0].sort_values("MarketValue", ascending=False)

        if comp.empty:
            st.caption("No market value.")
        else:
            total_mv = float(comp["MarketValue"].sum())
            comp["Perc"] = comp["MarketValue"] / total_mv
            
            pie = (
                alt.Chart(comp)
                .mark_arc(innerRadius=55, outerRadius=90)
                .encode(
                    theta="MarketValue:Q",
                    color=alt.Color("Underlying:N", title="Ticker"),
                    tooltip=[
                        alt.Tooltip("Underlying:N", title="Ticker"),
                        alt.Tooltip("MarketValue:Q", title="Value", format="$,.0f"),
                        alt.Tooltip("Perc:Q", title="% Portfolio", format=".1%")
                    ]
                )
                .properties(height=240)
            )
            st.altair_chart(pie, use_container_width=True)
            
            st.dataframe(
                comp[["Underlying", "Qty", "Last", "MarketValue"]]
                .rename(columns={
                    "Underlying": "Ticker", "Qty": "Shares", 
                    "Last": "Last", "MarketValue": "Value"
                })
                .assign(
                    Last=lambda d: d["Last"].round(2), 
                    Value=lambda d: d["Value"].round(2)
                ),
                use_container_width=True
            )

    # Option Expiries - NUOVA VERSIONE MIGLIORATA
    st.markdown("**Option Expiries (Contracts per Week)**")
    
    # Filtri temporali
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        show_week = st.button("Week", use_container_width=True, key="week_btn")
    with col2:
        show_month = st.button("Month", use_container_width=True, key="month_btn")
    with col3:
        show_year = st.button("Year", use_container_width=True, key="year_btn")
    with col4:
        show_all = st.button("All", use_container_width=True, key="all_btn")
    
    # Imposta timeframe default
    timeframe = "week"
    if show_month:
        timeframe = "month"
    elif show_year:
        timeframe = "year"
    elif show_all:
        timeframe = "all"
    
    if orders.empty:
        st.caption("No orders.")
    else:
        open_orders = orders[orders["Status"].astype(str).str.upper() == "OPEN"]
        if "Expiry" in open_orders.columns:
            open_orders["Expiry"] = pd.to_datetime(open_orders["Expiry"], errors="coerce")
            open_orders = open_orders.dropna(subset=["Expiry"])
            
            if not open_orders.empty:
                # Filtra per timeframe
                if timeframe == "week":
                    # Ultime 4 settimane
                    recent_date = open_orders["Expiry"].max()
                    cutoff_date = recent_date - pd.Timedelta(days=28)
                    filtered_orders = open_orders[open_orders["Expiry"] >= cutoff_date]
                    period_format = "W"
                    title_suffix = " (Last 4 Weeks)"
                elif timeframe == "month":
                    # Ultimi 6 mesi
                    recent_date = open_orders["Expiry"].max()
                    cutoff_date = recent_date - pd.Timedelta(days=180)
                    filtered_orders = open_orders[open_orders["Expiry"] >= cutoff_date]
                    period_format = "M"
                    title_suffix = " (Last 6 Months)"
                elif timeframe == "year":
                    # Ultimi 2 anni
                    recent_date = open_orders["Expiry"].max()
                    cutoff_date = recent_date - pd.Timedelta(days=730)
                    filtered_orders = open_orders[open_orders["Expiry"] >= cutoff_date]
                    period_format = "Q"
                    title_suffix = " (Last 2 Years)"
                else:  # all
                    filtered_orders = open_orders
                    period_format = "Q"
                    title_suffix = " (All Time)"
                
                if timeframe != "all":
                    filtered_orders = filtered_orders[filtered_orders["Expiry"] >= cutoff_date]
                
                if not filtered_orders.empty:
                    # Crea periodi in base al timeframe
                    if timeframe == "week":
                        filtered_orders["Period"] = filtered_orders["Expiry"].dt.to_period("W").dt.start_time
                        x_title = "Expiry Week"
                    elif timeframe == "month":
                        filtered_orders["Period"] = filtered_orders["Expiry"].dt.to_period("M").dt.start_time
                        x_title = "Expiry Month"
                    else:  # year e all
                        filtered_orders["Period"] = filtered_orders["Expiry"].dt.to_period("Q").dt.start_time
                        x_title = "Expiry Quarter"
                    
                    # Raggruppa i dati
                    grouped = filtered_orders.groupby(["Period", "Type"], as_index=False)["Qty"].sum()
                    
                    # Crea il grafico a barre sovrapposte
                    bar = alt.Chart(grouped).mark_bar().encode(
                        x=alt.X("Period:T", title=x_title),
                        y=alt.Y("Qty:Q", title="Contracts", stack=True),
                        color=alt.Color("Type:N", 
                                      title="Option Type",
                                      scale=alt.Scale(
                                          domain=["CALL", "PUT"],
                                          range=["#3b82f6", "#ef4444"]  # Blu per CALL, Rosso per PUT
                                      )),
                        tooltip=[
                            alt.Tooltip("Period:T", title=x_title, format="%Y-%m-%d"),
                            alt.Tooltip("Type:N", title="Type"),
                            alt.Tooltip("Qty:Q", title="Contracts")
                        ]
                    ).properties(
                        height=350,
                        title=f"Option Expiries by Volume{title_suffix}"
                    )
                    
                    st.altair_chart(bar, use_container_width=True)
                    
                    # Mostra anche una tabella riassuntiva
                    with st.expander("View Data Table"):
                        summary_table = grouped.pivot_table(
                            index="Period", 
                            columns="Type", 
                            values="Qty", 
                            aggfunc="sum", 
                            fill_value=0
                        ).reset_index()
                        summary_table["Period"] = summary_table["Period"].dt.strftime("%Y-%m-%d")
                        summary_table["Total"] = summary_table.sum(axis=1, numeric_only=True)
                        st.dataframe(summary_table, use_container_width=True)
                else:
                    st.caption(f"No open orders with expiry dates in the selected timeframe ({timeframe}).")
            else:
                st.caption("No open orders with valid expiry dates.")
        else:
            st.caption("Expiry column missing.")

    # Covered Call Coverage
    st.markdown("---")
    st.markdown("**Covered Call Coverage**")
    if positions.empty:
        st.caption("No positions.")
    else:
        pos_data = positions_nonzero(positions)
        pos_data["Underlying"] = pos_data["Underlying"].astype(str).map(clean_ticker)
        pos_data["CoveredSlots"] = (pos_data["Qty"] // 100)
        
        open_calls = pd.DataFrame(columns=["Underlying", "OpenCC"])
        if not orders.empty:
            open_call_orders = orders[
                (orders["Type"].str.upper() == "CALL") & 
                (orders["Status"].str.upper() == "OPEN")
            ].copy()
            open_call_orders["Underlying"] = open_call_orders["Underlying"].astype(str).map(clean_ticker)
            open_calls = open_call_orders.groupby("Underlying", as_index=False)["Qty"].sum().rename(columns={"Qty": "OpenCC"})
        
        coverage = pos_data.merge(open_calls, on="Underlying", how="left")
        coverage["OpenCC"] = coverage["OpenCC"].fillna(0).astype(int)
        coverage["FreeSlots"] = (coverage["CoveredSlots"] - coverage["OpenCC"]).clip(lower=0)
        
        melted = coverage.melt(
            id_vars=["Underlying"], 
            value_vars=["OpenCC", "FreeSlots"], 
            var_name="Slot", 
            value_name="Contracts"
        )
        
        bar = alt.Chart(melted).mark_bar().encode(
            x=alt.X("Underlying:N", sort="-y"),
            y="Contracts:Q",
            color=alt.Color("Slot:N", title="Status"),
            tooltip=["Underlying", "Slot", "Contracts"]
        ).properties(height=300)
        
        st.altair_chart(bar, use_container_width=True)

    # Moneyness by Contract
    st.markdown("**Moneyness by Contract (Current vs Strike, %)**")
    if orders.empty:
        st.caption("Need orders.")
    else:
        open_orders = orders[orders["Status"].astype(str).str.upper() == "OPEN"]
        if open_orders.empty:
            st.caption("No open orders.")
        else:
            moneyness_data = open_orders.copy()
            moneyness_data["Underlying"] = moneyness_data["Underlying"].astype(str).map(clean_ticker)
            moneyness_data["Strike"] = pd.to_numeric(moneyness_data["Strike"], errors="coerce")
            moneyness_data = moneyness_data.dropna(subset=["Strike"])
            moneyness_data["Last"] = moneyness_data["Underlying"].apply(lambda u: get_price_for(u) or 0.0)
            moneyness_data["PctFromStrike"] = (moneyness_data["Last"] - moneyness_data["Strike"]) / moneyness_data["Strike"]
            moneyness_data["AbsPct"] = moneyness_data["PctFromStrike"].abs()
            
            moneyness_data["Label"] = moneyness_data["Underlying"] + " " + moneyness_data["Strike"].astype(str)
            moneyness_data = moneyness_data.sort_values(["Underlying", "Strike"])

            max_abs = float(moneyness_data["AbsPct"].max() if not moneyness_data["AbsPct"].empty else 0.0)
            y_limit = max(0.1, min(0.25, (max_abs * 100) // 1 / 100 + 0.02))

            base = alt.Chart(moneyness_data).encode(
                x=alt.X(
                    "Label:N",
                    title="Ticker + Strike",
                    sort=alt.EncodingSortField(field="Underlying", order="ascending")
                ),
                y=alt.Y(
                    "PctFromStrike:Q",
                    title="% from Strike",
                    scale=alt.Scale(domain=[-y_limit, y_limit]),
                    axis=alt.Axis(format=".0%")
                ),
                color=alt.Color(
                    "Type:N",
                    title="Type",
                    scale=alt.Scale(
                        domain=["PUT", "CALL"],
                        range=["#3b82f6", "#10b981"]
                    )
                ),
                tooltip=[
                    alt.Tooltip("Underlying:N", title="Ticker"),
                    alt.Tooltip("Type:N", title="Type"),
                    alt.Tooltip("Strike:Q", title="Strike", format=",.2f"),
                    alt.Tooltip("Last:Q", title="Current", format=",.2f"),
                    alt.Tooltip("PctFromStrike:Q", title="% from Strike", format=".1%")
                ]
            )

            bars = base.mark_bar(size=18)
            labels = base.mark_text(
                dy=alt.ExprRef(expr="datum.PctFromStrike >= 0 ? -6 : 14"),
                fontSize=11
            ).encode(text=alt.Text("PctFromStrike:Q", format="+.1%"))

            zero_rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                strokeWidth=2, strokeDash=[6, 4]
            ).encode(y="y:Q")

            chart = (bars + labels + zero_rule).properties(height=320)
            st.altair_chart(chart, use_container_width=True)

    # Realized P/L
    st.markdown("---")
    st.markdown("**Realized P/L per Ticker**")
    realized_data = rebuild_realized_from_orders(orders)
    if realized_data.empty:
        st.caption("No realized P/L yet.")
    else:
        realized_summary = realized_data.copy()
        realized_summary["Underlying"] = realized_summary["Underlying"].astype(str).map(clean_ticker)
        aggregated = (realized_summary.groupby("Underlying", as_index=False)["TotalPL"]
                     .sum()
                     .sort_values("TotalPL", ascending=False))
        
        bar = alt.Chart(aggregated).mark_bar().encode(
            x=alt.X("Underlying:N", sort="-y"),
            y=alt.Y("TotalPL:Q", title="P/L"),
            color=alt.Color("TotalPL:Q", scale=alt.Scale(scheme='redyellowgreen')),
            tooltip=["Underlying", alt.Tooltip("TotalPL:Q", format=",.2f")]
        ).properties(height=300)
        
        st.altair_chart(bar, use_container_width=True)