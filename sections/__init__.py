# sections/__init__.py
from .dashboard import render_dashboard
from .portfolio import render_portfolio
from .positions import render_positions
from .analytics import render_analytics
from .cashflows import render_cashflows

__all__ = [
    'render_dashboard',
    'render_portfolio', 
    'render_positions',
    'render_analytics',
    'render_cashflows'
]