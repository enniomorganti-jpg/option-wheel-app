# pricing.py
import sys
import time
import random
import asyncio
from datetime import date
from typing import Dict, Tuple

import pandas as pd

from database import load_table, save_table, load_settings, save_settings
from utils import clean_ticker

# ===========================================
# Event loop & asyncio setup (robusto per Streamlit)
# ===========================================
# - Applica nest_asyncio una sola volta
# - Su Windows usa la WindowsSelectorEventLoopPolicy per compatibilità ib_insync
try:
    import nest_asyncio  # type: ignore
    nest_asyncio.apply()
except Exception:
    pass

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except Exception:
        pass

_LOOP_READY = False
def _ensure_loop():
    """Garantisce che ci sia un event loop attivo e riutilizzabile."""
    global _LOOP_READY
    try:
        asyncio.get_running_loop()
        _LOOP_READY = True
        return
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _LOOP_READY = True

# ===========================================
# IBKR setup
# ===========================================
IB_AVAILABLE = False
IB_IMPORT_ERR = ""
try:
    from ib_insync import IB, Stock, Contract  # type: ignore
    IB_AVAILABLE = True
except Exception as e:
    IB_IMPORT_ERR = f"{type(e).__name__}: {e}"

# ===========================================
# Prezzo singolo (lettura/scrittura)
# ===========================================
def get_price_for(ul: str) -> float | None:
    u = clean_ticker(ul)
    prices = load_table("prices.csv", ["Underlying", "Price", "AsOf"])
    row = prices.loc[prices["Underlying"].astype(str).str.upper() == u]
    if not row.empty:
        try:
            return float(row.sort_values("AsOf", ascending=False)["Price"].values[0])
        except Exception:
            return None
    return None

def set_price_for(ul: str, px: float) -> None:
    u = clean_ticker(ul)
    prices = load_table("prices.csv", ["Underlying", "Price", "AsOf"])
    prices = prices[prices["Underlying"].astype(str).str.upper() != u]
    prices = pd.concat(
        [prices, pd.DataFrame([{"Underlying": u, "Price": float(px), "AsOf": date.today()}])],
        ignore_index=True
    )
    save_table(prices, "prices.csv")

# ===========================================
# Yahoo Finance (fallback)
# ===========================================
def refresh_all_prices_yf(underlyings, max_retries: int = 3, base_sleep: float = 1.5) -> tuple[Dict[str, float], Dict[str, str]]:
    fetched: Dict[str, float] = {}
    errs: Dict[str, str] = {}
    syms = sorted({clean_ticker(u) for u in underlyings if clean_ticker(u)})

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}, {sym: "yfinance not installed" for sym in syms}

    for sym in syms:
        px = None
        reason = ""
        for attempt in range(1, max_retries + 1):
            try:
                t = yf.Ticker(sym)
                # fast_info prima
                try:
                    fi = getattr(t, "fast_info", None)
                    if fi and "last_price" in fi and fi["last_price"]:
                        px = float(fi["last_price"])
                except Exception:
                    px = None

                # fallback: history
                if px is None:
                    h = t.history(period="5d", interval="1d", auto_adjust=False)
                    if h is not None and not h.empty:
                        px = float(h["Close"].dropna().iloc[-1])

                if px is not None:
                    fetched[sym] = float(px)
                    break
                else:
                    raise RuntimeError("no price data")

            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
                if attempt < max_retries:
                    wait_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.7)
                    time.sleep(wait_s)
                else:
                    errs[sym] = reason
        # piccolo delay tra simboli per non farsi rate-limitar
        time.sleep(0.3 + random.uniform(0, 0.3))

    return fetched, errs

# ===========================================
# IBKR prices (con discovery + fallback YF)
# ===========================================
def refresh_prices_ibkr(underlyings, snapshot_wait: float = 2.5, use_yf_fallback: bool = True) -> tuple[Dict[str, float], Dict[str, str]]:
    """
    Aggiorna prezzi via IBKR provando:
      1) conId mappato (settings['ibkr_contract_map'])
      2) exchange hint (settings['ibkr_exchange_hints'])
      3) ContractDetails su SMART/NYSE/NASDAQ (auto-learn primaryExchange)
      4) Snapshot reqMktData -> reqTickers -> HistoricalData
    Opzionale fallback Yahoo per i mancanti.
    """
    _ensure_loop()

    fetched: Dict[str, float] = {}
    errs: Dict[str, str] = {}
    syms = sorted({clean_ticker(u) for u in underlyings if clean_ticker(u)})

    if not syms:
        return fetched, {"_": "no symbols"}

    if not IB_AVAILABLE:
        msg = "ib_insync not installed"
        if IB_IMPORT_ERR:
            msg += f" ({IB_IMPORT_ERR})"
        return {}, {"ib_insync": msg}

    s = load_settings() or {}
    ib_host = s.get("ibkr_host", "127.0.0.1")
    ib_port = int(s.get("ibkr_port", 7497))
    ib_cid  = int(s.get("ibkr_client_id", 1))

    exchange_hints: dict = (s.get("ibkr_exchange_hints", {}) or {})
    contract_map: dict   = (s.get("ibkr_contract_map", {}) or {})

    ib = IB()
    try:
        ib.connect(ib_host, ib_port, clientId=ib_cid)
    except Exception as e:
        return {}, {"connection": f"{type(e).__name__}: {e}"}

    try:
        # 1=RT, 2=Frozen, 3=Delayed, 4=DelayedFrozen — delayed robusto
        try:
            ib.reqMarketDataType(3)
            ib.sleep(0.6)
        except Exception:
            pass

        def _dedup_contracts(lst):
            seen = set()
            uniq = []
            for c in lst:
                key = f"{getattr(c,'conId',None)}|{getattr(c,'symbol',None)}|{getattr(c,'exchange',None)}|{getattr(c,'primaryExchange',None)}|{getattr(c,'currency',None)}"
                if key not in seen:
                    seen.add(key)
                    uniq.append(c)
            return uniq

        for sym in syms:
            px = None
            reason = ""

            # ---- prepara candidati
            candidates = []

            # 0) conId mappato
            mapped_conid = contract_map.get(sym.upper())
            if mapped_conid:
                candidates.append(Contract(conId=int(mapped_conid), secType="STK", currency="USD", exchange="SMART"))

            # 1) exchange hint
            hint = exchange_hints.get(sym.upper())
            if hint:
                candidates.append(Stock(sym, hint, "USD"))
                candidates.append(Stock(sym, "SMART", "USD", primaryExchange=hint))

            # 2) discovery
            try:
                cds = ib.reqContractDetails(Stock(sym, "SMART", "USD"))
                ib.sleep(0.2)
                if not cds:
                    for ex in ("NYSE", "NASDAQ"):
                        try:
                            cds = ib.reqContractDetails(Stock(sym, ex, "USD"))
                            ib.sleep(0.2)
                            if cds:
                                break
                        except Exception:
                            pass
                if cds:
                    for cd in cds:
                        c = cd.contract
                        if getattr(c, "currency", None) == "USD":
                            candidates.append(c)
                            pe = (getattr(c, "primaryExchange", "") or "").upper()
                            if pe and sym.upper() not in exchange_hints:
                                # auto-learn hint
                                try:
                                    ss = load_settings() or {}
                                    hints = (ss.get("ibkr_exchange_hints", {}) or {})
                                    hints[sym.upper()] = pe
                                    ss["ibkr_exchange_hints"] = hints
                                    save_settings(ss)
                                except Exception:
                                    pass
                            break
            except Exception:
                pass

            # 3) fallback SMART nudo
            candidates.append(Stock(sym, "SMART", "USD"))
            candidates = _dedup_contracts(candidates)

            # ---- tenta snapshot sui candidati
            for ctry in candidates:
                tkr = None
                try:
                    # qualify (necessario in molti casi)
                    q = ib.qualifyContracts(ctry)
                    if q:
                        ctry = q[0]
                    tkr = ib.reqMktData(ctry, "", True, False)  # snapshot=True
                    ib.sleep(snapshot_wait)
                    px = tkr.last or tkr.close or tkr.marketPrice()
                    if not px and getattr(tkr, "bid", None) and getattr(tkr, "ask", None):
                        px = (tkr.bid + tkr.ask) / 2.0
                except Exception as e:
                    reason = f"snapshot: {type(e).__name__}: {e}"
                finally:
                    if tkr is not None:
                        try:
                            ib.cancelMktData(tkr)
                        except Exception:
                            pass
                if px:
                    break

            # ---- reqTickers
            if not px and candidates:
                try:
                    ticks = ib.reqTickers(candidates[0])
                    if ticks:
                        tt = ticks[0]
                        px = tt.last or tt.close or tt.marketPrice()
                        if not px and getattr(tt, "bid", None) and getattr(tt, "ask", None):
                            px = (tt.bid + tt.ask) / 2.0
                except Exception as e:
                    reason = f"reqTickers: {type(e).__name__}: {e}"

            # ---- HistoricalData
            if not px and candidates:
                try:
                    bars = ib.reqHistoricalData(
                        candidates[0],
                        endDateTime="",
                        durationStr="2 D",
                        barSizeSetting="1 day",
                        whatToShow="TRADES",
                        useRTH=False,
                        keepUpToDate=False
                    )
                    if bars:
                        px = float(bars[-1].close)
                except Exception as e:
                    reason = f"history: {type(e).__name__}: {e}"

            if px is not None:
                fetched[sym] = float(px)
            else:
                errs[sym] = reason or "no price from IB"

            ib.sleep(0.25)  # respiro tra simboli

    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    # ---- Yahoo fallback
    if use_yf_fallback and errs:
        try:
            missing = list(errs.keys())
            f2, _ = refresh_all_prices_yf(missing)
            for k, v in f2.items():
                fetched[k] = float(v)
                if k in errs:
                    del errs[k]
        except Exception:
            pass

    return fetched, errs

# ===========================================
# Test connessione IBKR
# ===========================================
def test_ibkr_connection() -> tuple[bool, Dict[str, object]]:
    _ensure_loop()
    if not IB_AVAILABLE:
        msg = "ib_insync not installed"
        if IB_IMPORT_ERR:
            msg += f" ({IB_IMPORT_ERR})"
        return False, {"error": msg}

    s = load_settings() or {}
    ib = IB()
    try:
        ib.connect(
            s.get("ibkr_host", "127.0.0.1"),
            int(s.get("ibkr_port", 7497)),
            clientId=int(s.get("ibkr_client_id", 1)),
            timeout=5
        )
        accounts = ib.managedAccounts()
        ib.disconnect()
        return True, {"accounts": accounts}
    except Exception as e:
        try:
            ib.disconnect()
        except Exception:
            pass
        return False, {"error": f"{type(e).__name__}: {e}"}