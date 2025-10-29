# assets/themes.py
import streamlit as st
from string import Template

THEMES = {
    "Verde Robinhood": {
        "primary": "#00C805",
        "bg": "#F5F7F6",
        "panel": "#FFFFFF",
        "text": "#0B0D0C",
        "muted": "#0B0D0C",
        "accent": "#0B0D0C",
    },
    "Blu BBVA": {
        "primary": "#0B64C0",
        "bg": "#F6F8FB",
        "panel": "#FFFFFF",
        "text": "#0E1B2A",
        "muted": "#6B7A90",
        "accent": "#0079FF",
    },
    "Rosso HSBC/Generali": {
        "primary": "#DB0011",
        "bg": "#FBF6F6",
        "panel": "#FFFFFF",
        "text": "#1A1A1A",
        "muted": "#7A7A7A",
        "accent": "#A60010",
    },
    "Nero Trade Republic": {  # dark leggibile
        "primary": "#18A0FB",
        "bg": "#161819",
        "panel": "#1E2123",
        "text": "#EDEFF1",
        "muted": "#AEB5BA",
        "accent": "#8BC6FF",
    },
}

_CSS_TPL = Template("""
<style>
:root {
  --ow-primary: $primary;
  --ow-bg: $bg;
  --ow-panel: $panel;
  --ow-text: $text;
  --ow-muted: $muted;
  --ow-accent: $accent;
}

/* ===== Layout base ===== */
.stApp, .stApp [data-testid="stAppViewContainer"] {
  background: var(--ow-bg) !important;
  color: var(--ow-text) !important;
}
header[data-testid="stHeader"] {
  background: var(--ow-panel) !important;
  border-bottom: 1px solid rgba(0,0,0,0.06);
}
[data-testid="stSidebar"] > div:first-child {
  background: var(--ow-panel) !important;
  color: var(--ow-text) !important;
  border-right: 1px solid rgba(0,0,0,0.06);
}

/* ===== Titoli / testo ===== */
h1,h2,h3,h4,h5,h6 { color: var(--ow-text) !important; }
small, .stCaption, .stMarkdown p, .stMarkdown li { color: var(--ow-muted); }

/* ===== Tabs ===== */
[data-baseweb="tab-list"] { border-bottom: 1px solid rgba(0,0,0,0.08); }
button[role="tab"] {
  color: var(--ow-muted) !important;
  background: transparent !important;
  border: 0 !important;
}
button[role="tab"][aria-selected="true"] {
  color: var(--ow-text) !important;
  box-shadow: inset 0 -2px 0 0 var(--ow-primary) !important;
}

/* ===== Expander / pannelli ===== */
details, .streamlit-expanderHeader {
  background: var(--ow-panel) !important;
  color: var(--ow-text) !important;
  border: 1px solid rgba(0,0,0,0.06) !important;
  border-radius: 10px !important;
}

/* ===== Metric NO BOX, solo testo colorato ===== */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  padding: 0 !important;
}
[data-testid="stMetric"] [data-testid="stMetricLabel"] {
  color: var(--ow-muted) !important;
  font-weight: 600;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
  color: var(--ow-primary) !important;
}
[data-testid="stMetricDelta"] { color: var(--ow-accent) !important; }

/* ===== Tabelle ===== */
div[data-testid="stDataFrameResizable"] {
  background: var(--ow-panel) !important;
  border-radius: 10px;
  border: 1px solid rgba(0,0,0,0.06);
}

/* ===== Input ===== */
.stTextInput input, .stNumberInput input, .stDateInput input { color: var(--ow-text) !important; }
.stSelectbox > div:focus-within { outline: 2px solid var(--ow-primary) !important; }

/* ===== Bottoni neutri (no pieno) ===== */
.stButton > button, .stDownloadButton > button {
  background: #FFFFFF !important;
  color: var(--ow-text) !important;
  border: 1px solid rgba(0,0,0,0.12) !important;
  border-radius: 8px !important;
}
.stButton > button:hover, .stDownloadButton > button:hover {
  background: rgba(0,0,0,0.04) !important;
}
                    
/* Total Portfolio Value banner */
.tpv-wrap { margin: .5rem 0 1.25rem 0; text-align:center; }
.tpv-title { font-size: 1.35rem; font-weight: 500; opacity: .85; }
.tpv-value { font-size: 2.4rem; font-weight: 400; line-height: 1.2; }

</style>
""")

def apply_theme(name: str) -> None:
    theme = THEMES.get(name) or list(THEMES.values())[0]
    css = _CSS_TPL.substitute(theme)
    st.markdown(css, unsafe_allow_html=True)