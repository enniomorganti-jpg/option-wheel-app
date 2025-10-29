# assets/themes.py
import streamlit as st

# Palette per 3 temi "finance"
THEMES = {
    "Verde Robinhood": {
        "primary": "#1DB954",  # verde acceso
        "primary_dark": "#159A44",
        "bg": "#0F1115",
        "panel": "#151821",
        "text": "#E7ECEF",
        "muted": "#A6B1B8",
        "accent": "#2DE08A",
        "warn": "#E07A5F",
    },
    "Blu BBVA": {
        "primary": "#072146",  # blu BBVA
        "primary_dark": "#04152B",
        "bg": "#0B1830",
        "panel": "#0F2342",
        "text": "#EAF2FD",
        "muted": "#AFC6E9",
        "accent": "#00A3E0",
        "warn": "#F2B880",
    },
    "Nero Generali": {
        "primary": "#111111",
        "primary_dark": "#0B0B0B",
        "bg": "#0A0A0A",
        "panel": "#131313",
        "text": "#EDEDED",
        "muted": "#B9B9B9",
        "accent": "#C00000",  # rosso scuro
        "warn": "#E07A5F",
    },
}

_CSS_TEMPLATE = """
<style>
/* App background + testo base */
.stApp {{
  background: {bg};
  color: {text};
}}
/* Sidebar */
[data-testid="stSidebar"] > div:first-child {{
  background: {panel};
  border-right: 1px solid rgba(255,255,255,0.06);
}}
/* Titoli */
h1, h2, h3, h4, h5 {{
  color: {text};
}}
/* Tabs */
[data-baseweb="tab-list"] > div {{
  background: transparent !important;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}}
button[role="tab"] {{
  color: {muted} !important;
}}
button[role="tab"][aria-selected="true"] {{
  color: {text} !important;
  border-bottom: 2px solid {accent} !important;
}}
/* Pulsanti */
.stButton > button,
button[kind="primary"] {{
  background: {primary};
  color: #ffffff;
  border: 1px solid {primary_dark};
}}
.stButton > button:hover,
button[kind="primary"]:hover {{
  background: {primary_dark};
  border-color: {primary};
}}
/* Metric */
[data-testid="stMetric"] {{
  background: {panel};
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 12px;
  padding: 8px 12px;
}}
/* Dataframe (AgGrid-like) headers */
[data-testid="stDataFrame"] thead tr th {{
  background: {panel} !important;
  color: {text} !important;
  border-bottom: 1px solid rgba(255,255,255,0.08) !important;
}}
/* Expander */
.streamlit-expanderHeader {{
  background: {panel};
  color: {text};
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 8px;
}}
/* Divider line */
hr {{
  border-color: rgba(255,255,255,0.08);
}}
/* Warning/Info boxes */
.stAlert {{
  background: {panel};
  border: 1px solid rgba(255,255,255,0.08);
}}
/* Inputs */
.stTextInput > div > div > input,
.stNumberInput input,
.stDateInput input {{
  background: #0b0e14;
  color: {text};
  border: 1px solid rgba(255,255,255,0.12);
}}
/* Selectbox */
.stSelectbox > div > div {{
  background: #0b0e14;
  color: {text};
  border: 1px solid rgba(255,255,255,0.12);
}}
</style>
"""

def apply_theme(name: str) -> None:
    """
    Inietta CSS per il tema scelto.
    Va chiamata una sola volta (ad es. subito dopo aver letto la scelta in sidebar).
    """
    theme = THEMES.get(name) or list(THEMES.values())[0]
    css = _CSS_TEMPLATE.format(**theme)
    st.markdown(css, unsafe_allow_html=True)