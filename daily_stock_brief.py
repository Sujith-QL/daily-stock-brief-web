"""
daily_stock_brief.py

Everything in one file: config, indicators, rules engine, data fetcher,
report generator, and the main run function.

No broker account, no paid subscription, no API key needed - uses free
public market data (via yfinance).

HOW TO RUN:
    pip install pandas yfinance
    python daily_stock_brief.py

It will fetch Nifty 50 data, run the analysis, print the report, and save
it to daily_report.txt in the same folder.
"""

import csv
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import yfinance as yf


# =====================================================================
# CONFIG - tweak these to change what gets flagged and how aggressively
# =====================================================================

NIFTY_50 = sorted(set([
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS", "AXISBANK.NS",
    "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "BEL.NS", "BHARTIARTL.NS",
    "CIPLA.NS", "COALINDIA.NS", "DRREDDY.NS", "EICHERMOT.NS", "GRASIM.NS",
    "HCLTECH.NS", "HDFCBANK.NS", "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS",
    "HINDUNILVR.NS", "ICICIBANK.NS", "ITC.NS", "INDUSINDBK.NS", "INFY.NS",
    "JSWSTEEL.NS", "KOTAKBANK.NS", "LT.NS", "M&M.NS", "MARUTI.NS",
    "NTPC.NS", "NESTLEIND.NS", "ONGC.NS", "POWERGRID.NS", "RELIANCE.NS",
    "SBILIFE.NS", "SHRIRAMFIN.NS", "SBIN.NS", "SUNPHARMA.NS", "TCS.NS",
    "TATACONSUM.NS", "TMPV.NS", "TMCV.NS", "TATASTEEL.NS", "TECHM.NS", "TITAN.NS",
    "TRENT.NS", "ULTRACEMCO.NS", "WIPRO.NS",
]))

# Nifty Next 50 - sourced from NSE's official factsheet (via Wikipedia, dated
# April 2026). Combined with Nifty 50 below, this gives a ~100-stock universe
# (Nifty 100) instead of just 50, so there are more candidates for each
# category (Intraday/Short Term/Long Term) to find on any given day.
# Two symbols from the source table looked possibly garbled/uncertain and
# were left out rather than guessed at.
NIFTY_NEXT_50 = [
    "ABB.NS", "ADANIENSOL.NS", "ADANIGREEN.NS", "ADANIPOWER.NS", "AMBUJACEM.NS",
    "BAJAJHLDNG.NS", "BANKBARODA.NS", "BPCL.NS", "BRITANNIA.NS", "BOSCHLTD.NS",
    "CANBK.NS", "CGPOWER.NS", "CHOLAFIN.NS", "CUMMINSIND.NS", "DIVISLAB.NS",
    "DLF.NS", "DMART.NS", "GAIL.NS", "GODREJCP.NS", "HDFCAMC.NS",
    "HAL.NS", "HINDZINC.NS", "HYUNDAI.NS", "INDHOTEL.NS", "IOC.NS",
    "IRFC.NS", "JINDALSTEL.NS", "LODHA.NS", "MAZDOCK.NS", "MUTHOOTFIN.NS",
    "PIDILITIND.NS", "PFC.NS", "PNB.NS", "RECLTD.NS", "MOTHERSON.NS",
    "SHREECEM.NS", "SIEMENS.NS", "SOLARINDS.NS", "TATACAP.NS", "TATAPOWER.NS",
    "TORNTPHARM.NS", "TVSMOTOR.NS", "UNIONBANK.NS", "UNITDSPR.NS", "VBL.NS",
    "VEDL.NS", "ZYDUSLIFE.NS",
]

# The combined scanning universe used by the app. If a symbol ever fails to
# fetch (e.g. delisted, renamed), fetch_all() already skips it gracefully -
# it won't break the report, that stock just won't appear that day.
WATCHLIST = sorted(set(NIFTY_50 + NIFTY_NEXT_50))

HISTORY_DAYS = 300           # how many days of history to pull (need ~250+ for 200-day MA)

SWING_FAST_MA = 20
SWING_SLOW_MA = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70          # avoid flagging stocks already this "hot"
RSI_OVERSOLD = 30

POSITIONAL_FAST_MA = 50
POSITIONAL_SLOW_MA = 200

VOLUME_SPIKE_MULTIPLIER = 1.5
INTRADAY_TARGET_RR = 1.5     # target = this many multiples of the stoploss distance
SWING_TARGET_RR = 2.0
POSITIONAL_TARGET_RR = 3.0

SR_LOOKBACK = 20             # support/resistance lookback window, in trading days

# Human-readable company names, used for better news search results than raw
# ticker symbols. Add to this if you add stocks beyond the Nifty 50 list.
COMPANY_NAMES = {
    "ADANIENT.NS": "Adani Enterprises", "ADANIPORTS.NS": "Adani Ports",
    "APOLLOHOSP.NS": "Apollo Hospitals", "ASIANPAINT.NS": "Asian Paints",
    "AXISBANK.NS": "Axis Bank", "BAJAJ-AUTO.NS": "Bajaj Auto",
    "BAJFINANCE.NS": "Bajaj Finance", "BAJAJFINSV.NS": "Bajaj Finserv",
    "BEL.NS": "Bharat Electronics", "BHARTIARTL.NS": "Bharti Airtel",
    "CIPLA.NS": "Cipla", "COALINDIA.NS": "Coal India", "DRREDDY.NS": "Dr Reddy's",
    "EICHERMOT.NS": "Eicher Motors", "GRASIM.NS": "Grasim Industries",
    "HCLTECH.NS": "HCL Technologies", "HDFCBANK.NS": "HDFC Bank",
    "HDFCLIFE.NS": "HDFC Life", "HEROMOTOCO.NS": "Hero MotoCorp",
    "HINDALCO.NS": "Hindalco", "HINDUNILVR.NS": "Hindustan Unilever",
    "ICICIBANK.NS": "ICICI Bank", "ITC.NS": "ITC", "INDUSINDBK.NS": "IndusInd Bank",
    "INFY.NS": "Infosys", "JSWSTEEL.NS": "JSW Steel", "KOTAKBANK.NS": "Kotak Mahindra Bank",
    "LT.NS": "Larsen & Toubro", "M&M.NS": "Mahindra & Mahindra", "MARUTI.NS": "Maruti Suzuki",
    "NTPC.NS": "NTPC", "NESTLEIND.NS": "Nestle India", "ONGC.NS": "ONGC",
    "POWERGRID.NS": "Power Grid Corporation", "RELIANCE.NS": "Reliance Industries",
    "SBILIFE.NS": "SBI Life Insurance", "SHRIRAMFIN.NS": "Shriram Finance",
    "SBIN.NS": "State Bank of India", "SUNPHARMA.NS": "Sun Pharma", "TCS.NS": "Tata Consultancy Services",
    "TATACONSUM.NS": "Tata Consumer Products", "TMPV.NS": "Tata Motors Passenger Vehicles", "TMCV.NS": "Tata Motors (Commercial Vehicles)",
    "TATASTEEL.NS": "Tata Steel", "TECHM.NS": "Tech Mahindra", "TITAN.NS": "Titan Company",
    "TRENT.NS": "Trent", "ULTRACEMCO.NS": "UltraTech Cement", "WIPRO.NS": "Wipro",
    "ABB.NS": "ABB India", "ADANIENSOL.NS": "Adani Energy Solutions",
    "ADANIGREEN.NS": "Adani Green Energy", "ADANIPOWER.NS": "Adani Power",
    "AMBUJACEM.NS": "Ambuja Cements", "BAJAJHLDNG.NS": "Bajaj Holdings",
    "BANKBARODA.NS": "Bank of Baroda", "BPCL.NS": "Bharat Petroleum",
    "BRITANNIA.NS": "Britannia Industries", "BOSCHLTD.NS": "Bosch",
    "CANBK.NS": "Canara Bank", "CGPOWER.NS": "CG Power",
    "CHOLAFIN.NS": "Cholamandalam Investment", "CUMMINSIND.NS": "Cummins India",
    "DIVISLAB.NS": "Divi's Laboratories", "DLF.NS": "DLF",
    "DMART.NS": "Avenue Supermarts DMart", "GAIL.NS": "GAIL India",
    "GODREJCP.NS": "Godrej Consumer Products", "HDFCAMC.NS": "HDFC Asset Management",
    "HAL.NS": "Hindustan Aeronautics", "HINDZINC.NS": "Hindustan Zinc",
    "HYUNDAI.NS": "Hyundai Motor India", "INDHOTEL.NS": "Indian Hotels Company",
    "IOC.NS": "Indian Oil Corporation", "IRFC.NS": "IRFC",
    "JINDALSTEL.NS": "Jindal Steel", "LODHA.NS": "Lodha",
    "MAZDOCK.NS": "Mazagon Dock Shipbuilders", "MUTHOOTFIN.NS": "Muthoot Finance",
    "PIDILITIND.NS": "Pidilite Industries", "PFC.NS": "Power Finance Corporation",
    "PNB.NS": "Punjab National Bank", "RECLTD.NS": "REC Limited",
    "MOTHERSON.NS": "Samvardhana Motherson", "SHREECEM.NS": "Shree Cement",
    "SIEMENS.NS": "Siemens India", "SOLARINDS.NS": "Solar Industries",
    "TATACAP.NS": "Tata Capital", "TATAPOWER.NS": "Tata Power",
    "TORNTPHARM.NS": "Torrent Pharmaceuticals", "TVSMOTOR.NS": "TVS Motor Company",
    "UNIONBANK.NS": "Union Bank of India", "UNITDSPR.NS": "United Spirits",
    "VBL.NS": "Varun Beverages", "VEDL.NS": "Vedanta", "ZYDUSLIFE.NS": "Zydus Lifesciences",
}

SEARCH_HISTORY_DAYS = 400     # extra buffer over HISTORY_DAYS so 52-week high/low is meaningful

NEWS_PER_STOCK = 2            # how many headlines to attach per flagged stock
NEWS_TIMEOUT_SECONDS = 8

IPO_WATCHLIST_FILE = "ipo_watchlist.csv"   # you update this file yourself (see below)
IPO_STRONG_THRESHOLD_PCT = 20   # GMP as % of issue price >= this -> "Strong"
IPO_MODERATE_THRESHOLD_PCT = 8  # >= this -> "Moderate", below -> "Weak", negative -> "Avoid"


# =====================================================================
# INDICATORS - pure calculation functions
# =====================================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average - the plain average of the last `period` values."""
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (0-100). Above ~70 = "overbought" (may be due a
    pullback). Below ~30 = "oversold" (may be due a bounce).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    result = 100 - (100 / (1 + rs))
    return result.fillna(100)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range - typical daily move size. Used to size stoplosses
    sensibly (volatile stocks need wider stops than calm ones).
    """
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=period).mean()


def recent_support_resistance(df: pd.DataFrame, lookback: int = 20):
    """Lowest low / highest high over the lookback window (excluding today)."""
    window = df.iloc[-(lookback + 1):-1]
    return window["Low"].min(), window["High"].max()


# =====================================================================
# RULES ENGINE - turns indicators into Entry / Stoploss / Target calls
# =====================================================================

MIN_RESULTS_PER_CATEGORY = 10  # we aim to always show this many, ranked best-first


def analyze_swing(symbol: str, df: pd.DataFrame):
    """
    Short-term uptrend candidate: fast MA above slow MA. Returns a candidate
    with a rank_score (lower = better = more recently crossed) rather than a
    strict pass/fail - this lets the caller always show the best N candidates
    across the whole watchlist, instead of an empty section on a quiet day.
    """
    if len(df) < SWING_SLOW_MA + 40:
        return None

    fast_ma = sma(df["Close"], SWING_FAST_MA)
    slow_ma = sma(df["Close"], SWING_SLOW_MA)
    rsi_series = rsi(df["Close"], RSI_PERIOD)

    fast_today, slow_today = fast_ma.iloc[-1], slow_ma.iloc[-1]
    rsi_today = rsi_series.iloc[-1]

    currently_bullish = fast_today > slow_today
    if not currently_bullish:
        return None
    if rsi_today >= RSI_OVERBOUGHT or rsi_today <= RSI_OVERSOLD:
        return None

    # How many trading days ago did the crossover happen? Search back up to
    # 60 days; if not found in that window, treat it as an established trend
    # (still a valid candidate, just ranked behind fresher crossovers).
    max_lookback = 60
    crossed_up = (fast_ma.shift(1) <= slow_ma.shift(1)) & (fast_ma > slow_ma)
    recent = crossed_up.iloc[-max_lookback:]
    cross_positions = recent[recent].index
    if len(cross_positions) > 0:
        days_since_cross = len(df.loc[cross_positions[-1]:]) - 1
    else:
        days_since_cross = max_lookback  # established trend, not a fresh signal

    close = df["Close"].iloc[-1]
    support, _ = recent_support_resistance(df, SR_LOOKBACK)

    entry = close
    stoploss = min(support, entry * 0.97)
    risk = entry - stoploss
    if risk <= 0:
        return None
    target = entry + risk * SWING_TARGET_RR

    return {
        "symbol": symbol, "style": "Swing",
        "direction": "Bullish",
        "entry": round(entry, 2), "stoploss": round(stoploss, 2), "target": round(target, 2),
        "risk_reward": SWING_TARGET_RR,
        "reason": (
            f"{SWING_FAST_MA}-day MA crossed above {SWING_SLOW_MA}-day MA "
            f"{days_since_cross} trading day{'s' if days_since_cross != 1 else ''} ago, "
            f"RSI {rsi_today:.0f} (not overbought)"
        ),
        "rank_score": days_since_cross,  # lower = fresher = better
    }


def analyze_positional(symbol: str, df: pd.DataFrame):
    """
    Longer-term uptrend candidate: 50-day MA above 200-day MA, price above
    both. Ranked by trend strength (how far apart the MAs have separated) -
    a wider gap suggests a more established, confirmed trend.
    """
    if len(df) < POSITIONAL_SLOW_MA + 5:
        return None

    fast_ma = sma(df["Close"], POSITIONAL_FAST_MA)
    slow_ma = sma(df["Close"], POSITIONAL_SLOW_MA)
    atr_today = atr(df, 14).iloc[-1]

    fast_today = fast_ma.iloc[-1]
    slow_today = slow_ma.iloc[-1]
    close = df["Close"].iloc[-1]

    if not ((fast_today > slow_today) and (close > fast_today)):
        return None

    trend_strength_pct = (fast_today - slow_today) / slow_today * 100

    entry = close
    stoploss = entry - (atr_today * 2.5)  # volatility-based, wider stop for longer holds
    risk = entry - stoploss
    if risk <= 0:
        return None
    target = entry + risk * POSITIONAL_TARGET_RR

    return {
        "symbol": symbol, "style": "Positional",
        "direction": "Bullish",
        "entry": round(entry, 2), "stoploss": round(stoploss, 2), "target": round(target, 2),
        "risk_reward": POSITIONAL_TARGET_RR,
        "reason": (
            f"Price above rising {POSITIONAL_FAST_MA}/{POSITIONAL_SLOW_MA}-day MAs "
            f"(trend strength {trend_strength_pct:.1f}%), stop sized on volatility (ATR)"
        ),
        "rank_score": -trend_strength_pct,  # more negative = stronger trend = ranks first
    }


def analyze_intraday_watch(symbol: str, df: pd.DataFrame):
    """
    Pre-market watchlist candidate (not a live trigger, since we only have
    yesterday's data before the open): ranks stocks by how close they are to
    breaking their recent resistance - closer candidates rank first.

    This only flags bullish (buy-side) setups - see analyze_stock module
    docstring for why breakdown/short setups are intentionally excluded.
    """
    if len(df) < SR_LOOKBACK + 5:
        return None

    close = df["Close"].iloc[-1]
    support, resistance = recent_support_resistance(df, SR_LOOKBACK)
    atr_today = atr(df, 14).iloc[-1]

    # Sanity check: must be trading within its recent range, not already
    # breaking down below support (that's a different, non-buy situation).
    if close <= support:
        return None

    distance_to_resistance = resistance - close
    distance_pct = (distance_to_resistance / close) * 100

    entry = round(resistance * 1.001, 2)
    stoploss = round(resistance - atr_today * 0.5, 2)
    risk = entry - stoploss
    if risk <= 0:
        return None
    target = round(entry + risk * INTRADAY_TARGET_RR, 2)

    return {
        "symbol": symbol, "style": "Intraday (watch)",
        "direction": "Bullish",
        "entry": entry, "stoploss": stoploss, "target": target,
        "risk_reward": INTRADAY_TARGET_RR,
        "reason": (
            f"Trading {distance_pct:.1f}% below {SR_LOOKBACK}-day resistance "
            f"({resistance:.2f}) - watch for breakout above at open"
        ),
        "rank_score": distance_pct,  # lower = closer to breakout = better
    }


def analyze_stock(symbol: str, df: pd.DataFrame) -> list:
    """Run all three styles against one stock's data, return whichever produced a candidate."""
    results = []
    for fn in (analyze_intraday_watch, analyze_swing, analyze_positional):
        try:
            signal = fn(symbol, df)
        except Exception:
            signal = None
        if signal:
            results.append(signal)
    return results


def fetch_live_prices(symbols: list) -> dict:
    """
    Fetches current/latest live price for a batch of symbols. Used to sanity
    check every candidate signal against what's actually happening right now,
    not just yesterday's close - a stock can look like a clean setup based on
    yesterday's data and already be breaking down by the time you see the
    report, especially on news-driven moves.
    """
    if not symbols:
        return {}
    prices = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for symbol in symbols:
            try:
                t = tickers.tickers.get(symbol)
                if t is None:
                    continue
                fast = t.fast_info
                price = fast.get("lastPrice") or fast.get("last_price")
                if price:
                    prices[symbol] = round(float(price), 2)
            except Exception as e:
                print(f"  [!] Live price fetch failed for {symbol}: {e}")
    except Exception as e:
        print(f"  [!] Batch live price fetch failed: {e}")
    return prices


def select_top_signals(all_candidates: list, n: int = MIN_RESULTS_PER_CATEGORY,
                        live_prices: dict = None) -> list:
    """
    Takes the full pool of candidates (across all scanned stocks, all styles),
    ranks by rank_score (best first), and validates each one against a live
    price before including it:
      - If the live price has already fallen to/through the calculated
        stoploss, the setup is already invalidated by today's actual trading -
        it's skipped entirely, and we backfill from the next-ranked candidate
        instead, so we still aim for N results without showing a broken setup.
      - If the live price has slipped below entry but not yet hit stoploss,
        we keep it but flag it clearly so you can see the setup is weakening
        in real time, rather than silently showing a stale number.
    If live_prices is None or a symbol's live price can't be fetched, that
    candidate is kept as-is with no live comparison (never silently dropped
    just because a live quote wasn't available).
    """
    live_prices = live_prices or {}
    by_style: dict[str, list] = {}
    for c in all_candidates:
        by_style.setdefault(c["style"], []).append(c)

    selected = []
    for style, candidates in by_style.items():
        candidates.sort(key=lambda c: c["rank_score"])
        kept = 0
        for c in candidates:
            if kept >= n:
                break
            c = dict(c)
            c.pop("rank_score", None)
            live = live_prices.get(c["symbol"])
            if live is not None:
                c["live_price"] = live
                if live <= c["stoploss"]:
                    # Already invalidated by today's actual trading - skip,
                    # backfill from the next candidate instead of showing it.
                    continue
                if live < c["entry"]:
                    c["caution"] = (
                        f"Live price (\u20b9{live}) has already slipped below the "
                        f"calculated entry (\u20b9{c['entry']}) - setup may be weakening"
                    )
            selected.append(c)
            kept += 1
    return selected


# =====================================================================
# DATA FETCHER - free public data, no broker account needed
# =====================================================================

def fetch_history(symbol: str, days: int = None):
    days = days or HISTORY_DAYS
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception as e:
        print(f"  [!] Failed to fetch {symbol}: {e}")
        return None


def fetch_all(symbols: list, pause_seconds: float = 0.3) -> dict:
    data = {}
    total = len(symbols)
    for i, symbol in enumerate(symbols, start=1):
        print(f"  Fetching {symbol} ({i}/{total})...")
        df = fetch_history(symbol)
        if df is not None and len(df) > 30:
            data[symbol] = df
        time.sleep(pause_seconds)
    return data



# =====================================================================
# FULL-MARKET SCAN UNIVERSE - every main-board NSE (EQ/BE series) stock,
# plus main-board BSE stocks that are NOT also listed on NSE. SME-platform
# stocks are excluded. The list is rebuilt from the exchanges' own public
# files at most once every UNIVERSE_REFRESH_DAYS and cached on disk; if the
# download fails, the last cached copy (or, failing that, the Nifty 100
# WATCHLIST above) is used so a scan never silently produces nothing.
# =====================================================================
UNIVERSE_CACHE_FILE = "scan_universe.json"
UNIVERSE_REFRESH_DAYS = 7
NSE_MAIN_BOARD_SERIES = {"EQ", "BE"}          # SM / ST etc. = SME platform, skipped
BSE_MAIN_BOARD_GROUPS = {"A", "B", "T", "X", "XT", "Z"}   # M / MT / MS = SME, skipped
INCLUDE_BSE_ONLY_STOCKS = False        # BSE-only listings are mostly illiquid; skipping them saves a lot of scan time
# "nifty500" = scan the ~500 largest/most liquid NSE stocks (fast, fits a free server)
# "all"      = scan every NSE main-board stock (slow; needs a stronger server)
# Override without editing code:  set environment variable SCAN_UNIVERSE=all
SCAN_UNIVERSE_MODE = os.environ.get("SCAN_UNIVERSE", "nifty500").strip().lower()

# Liquidity filter - without it, thousands of illiquid micro-caps would
# crowd the ranked lists with signals nobody can actually trade.
MIN_AVG_DAILY_TRADED_VALUE_INR = 2e7   # ~Rs 2 crore average over the last 20 sessions
MIN_SCAN_PRICE_INR = 20
MAX_DATA_STALENESS_DAYS = 7            # drop suspended / delisted stocks
SCAN_CHUNK_SIZE = 250                  # symbols per Yahoo batch request
SCAN_CHUNK_PAUSE_SECONDS = 0.5

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_nse_equity_list() -> list:
    """[(symbol, name, isin)] from NSE's public EQUITY_L.csv (main-board series only)."""
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    rows = csv.DictReader(resp.text.splitlines())
    out = []
    for row in rows:
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        if row.get("SERIES") not in NSE_MAIN_BOARD_SERIES or not row.get("SYMBOL"):
            continue
        out.append((row["SYMBOL"], row.get("NAME OF COMPANY") or row["SYMBOL"], row.get("ISIN NUMBER", "")))
    return out


def _fetch_bse_equity_list() -> list:
    """[(scrip_code, name, isin)] of active main-board BSE equities (best effort)."""
    url = "https://api.bseindia.com/BseIndiaAPI/api/ListOfScripData/w"
    params = {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"}
    headers = dict(_BROWSER_HEADERS, Referer="https://www.bseindia.com/", Origin="https://www.bseindia.com")
    resp = requests.get(url, params=params, headers=headers, timeout=45)
    resp.raise_for_status()
    out = []
    for item in resp.json():
        group = (item.get("GROUP") or item.get("Group") or "").strip().upper()
        code = str(item.get("SCRIP_CD") or item.get("Scrip_Cd") or "").strip()
        if group not in BSE_MAIN_BOARD_GROUPS or not code:
            continue
        name = item.get("Scrip_Name") or item.get("Issuer_Name") or item.get("scrip_id") or code
        out.append((code, name.strip(), (item.get("ISIN_NUMBER") or item.get("ISIN") or "").strip()))
    return out


def build_scan_universe() -> dict:
    """Downloads the exchange lists and returns {symbols, names, counts}."""
    nse_rows = _fetch_nse_equity_list()
    symbols = [f"{sym}.NS" for sym, _, _ in nse_rows]
    names = {f"{sym}.NS": name for sym, name, _ in nse_rows}
    nse_isins = {isin for _, _, isin in nse_rows if isin}
    bse_added = 0
    if INCLUDE_BSE_ONLY_STOCKS:
        try:
            for code, name, isin in _fetch_bse_equity_list():
                if isin and isin in nse_isins:
                    continue                      # already scanned via NSE
                symbols.append(f"{code}.BO")
                names[f"{code}.BO"] = name
                bse_added += 1
        except Exception as e:
            print(f"  [!] BSE list fetch failed (continuing with NSE only): {e}")
    return {
        "built_on": date.today().isoformat(),
        "symbols": sorted(set(symbols)),
        "names": names,
        "counts": {"nse": len(nse_rows), "bse_only": bse_added},
    }


def _fetch_nifty500_symbols() -> set:
    """Set of 'SYMBOL.NS' for the Nifty 500 index (NSE's public constituents CSV)."""
    urls = [
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ]
    last_err = None
    for url in urls:
        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
            resp.raise_for_status()
            rows = csv.DictReader(resp.text.splitlines())
            out = set()
            for row in rows:
                row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
                if row.get("SYMBOL"):
                    out.add(f"{row['SYMBOL']}.NS")
            if len(out) > 300:
                return out
        except Exception as e:
            last_err = e
    raise RuntimeError(f"could not fetch Nifty 500 list ({last_err})")


def get_scan_universe(force_refresh: bool = False) -> list:
    """Cached full-market symbol list (see block comment above)."""
    cached = None
    if os.path.exists(UNIVERSE_CACHE_FILE):
        try:
            with open(UNIVERSE_CACHE_FILE) as f:
                cached = json.load(f)
        except Exception:
            cached = None
    fresh_enough = False
    if cached:
        try:
            age = (date.today() - date.fromisoformat(cached["built_on"])).days
            fresh_enough = age < UNIVERSE_REFRESH_DAYS
        except Exception:
            pass
    if cached is None or force_refresh or not fresh_enough:
        try:
            built = build_scan_universe()
            with open(UNIVERSE_CACHE_FILE, "w") as f:
                json.dump(built, f)
            cached = built
            print(f"  Universe rebuilt: {built['counts']}")
        except Exception as e:
            print(f"  [!] Could not rebuild stock universe ({e}); using "
                  f"{'cached copy' if cached else 'Nifty 100 WATCHLIST fallback'}")
    if not cached:
        return list(WATCHLIST)
    for sym, name in cached.get("names", {}).items():
        COMPANY_NAMES.setdefault(sym, name)       # lets search/labels know every company
    symbols = cached["symbols"]
    if not INCLUDE_BSE_ONLY_STOCKS:
        symbols = [x for x in symbols if not x.endswith(".BO")]
    if SCAN_UNIVERSE_MODE != "all":
        try:
            top = _fetch_nifty500_symbols()
            narrowed = [x for x in symbols if x in top]
            if len(narrowed) > 300:
                print(f"  Scan universe narrowed to Nifty 500: {len(narrowed)} symbols")
                return narrowed
        except Exception as e:
            print(f"  [!] Nifty 500 narrowing skipped ({e}); scanning the full list")
    return symbols


def _passes_liquidity(df: pd.DataFrame) -> bool:
    if len(df) <= 30:
        return False
    last_day = df.index[-1]
    last_day = last_day.date() if hasattr(last_day, "date") else last_day
    if (date.today() - last_day).days > MAX_DATA_STALENESS_DAYS:
        return False
    if float(df["Close"].iloc[-1]) < MIN_SCAN_PRICE_INR:
        return False
    traded_value = (df["Close"] * df["Volume"]).tail(20).mean()
    return bool(traded_value >= MIN_AVG_DAILY_TRADED_VALUE_INR)


def fetch_all_batched(symbols: list, chunk_size: int = SCAN_CHUNK_SIZE,
                      pause_seconds: float = SCAN_CHUNK_PAUSE_SECONDS, progress_cb=None) -> dict:
    """
    Same output as fetch_all() ({symbol: DataFrame}) but downloads
    chunk_size symbols per request - thousands of stocks in minutes instead
    of an hour - and drops illiquid/stale ones. progress_cb(done, total) is
    called after every chunk so the UI can show progress.
    """
    data = {}
    total = len(symbols)
    for start in range(0, total, chunk_size):
        chunk = symbols[start:start + chunk_size]
        raw = None
        for attempt in range(2):                  # one retry in case Yahoo throttles
            try:
                raw = yf.download(chunk, period=f"{HISTORY_DAYS}d", interval="1d", progress=False,
                                  auto_adjust=True, group_by="ticker", threads=True)
            except Exception as e:
                print(f"  [!] Batch {start}-{start + len(chunk)} failed: {e}")
                raw = None
            if raw is not None and not raw.empty:
                break
            time.sleep(20)
        if raw is not None and not raw.empty:
            multi = isinstance(raw.columns, pd.MultiIndex)
            for sym in chunk:
                try:
                    if multi:
                        if sym not in raw.columns.get_level_values(0):
                            continue
                        df = raw[sym]
                    else:
                        df = raw
                    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                except Exception:
                    continue
                if _passes_liquidity(df):
                    data[sym] = df
        done = min(start + chunk_size, total)
        print(f"  Scanned {done}/{total} symbols - {len(data)} liquid stocks kept so far")
        if progress_cb:
            progress_cb(done, total)
        time.sleep(pause_seconds)
    return data


# =====================================================================
# STOCK SEARCH - free-text lookup of ANY NSE/BSE-listed stock, not just
# the fixed watchlist above. Reuses the same rule engines (analyze_swing,
# analyze_positional, analyze_intraday_watch) so a searched stock gets the
# exact same Entry/Stoploss/Target logic as the daily scan - it just isn't
# restricted to the Nifty 100 universe.
# =====================================================================

def find_symbol_candidates(query: str) -> list:
    """
    Matches free-text input against the known watchlist/name map first, so
    common tickers and company names resolve instantly and unambiguously.
    Returns [] if nothing known matches - the caller then falls back to
    treating the input as a raw ticker and trying to fetch it directly
    (the known list only covers ~95 large-caps; NSE/BSE list thousands more).
    """
    q = query.strip()
    if not q:
        return []
    q_ticker = q.upper().replace(" ", "")
    q_name = q.strip().lower()

    candidates = {}

    # 1) Exact ticker match (with or without .NS) against the known watchlist.
    for symbol in WATCHLIST:
        base = symbol.replace(".NS", "")
        if base == q_ticker or symbol == q_ticker:
            candidates[symbol] = COMPANY_NAMES.get(symbol, base)
    if candidates:
        return [{"symbol": s, "name": n} for s, n in candidates.items()]

    # 2) Company-name substring match (only if no exact ticker hit above),
    # e.g. "tata motors" -> TATAMOTORS.NS.
    if len(q_name) >= 3:
        for symbol, name in COMPANY_NAMES.items():
            if q_name in name.lower():
                candidates[symbol] = name

    return [{"symbol": s, "name": n} for s, n in candidates.items()]


def normalize_manual_symbol(query: str) -> str:
    """Best-guess Yahoo Finance symbol for free-typed input with no known match."""
    q = query.strip().upper().replace(" ", "")
    if q.endswith(".NS") or q.endswith(".BO"):
        return q
    return q + ".NS"


def build_snapshot(df: pd.DataFrame) -> dict:
    """
    A general "current status" readout for a single stock - independent of
    whether any of the three rule engines currently produce a setup, so a
    search always returns something useful even on a stock that's flat or
    in a downtrend.
    """
    close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else close
    day_change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    rsi_today = rsi(df["Close"], RSI_PERIOD).iloc[-1]
    ma20 = sma(df["Close"], 20).iloc[-1]
    ma50 = sma(df["Close"], 50).iloc[-1]
    ma200 = sma(df["Close"], 200).iloc[-1] if len(df) >= 200 else None

    vol_today = df["Volume"].iloc[-1]
    vol_avg20 = df["Volume"].rolling(20).mean().iloc[-1]
    vol_ratio = round(float(vol_today / vol_avg20), 2) if pd.notna(vol_avg20) and vol_avg20 else None

    lookback = df.tail(252) if len(df) >= 252 else df
    week_high = round(float(lookback["High"].max()), 2)
    week_low = round(float(lookback["Low"].min()), 2)

    ma50_valid = ma50 is not None and not pd.isna(ma50)
    ma200_valid = ma200 is not None and not pd.isna(ma200)
    ma20_valid = ma20 is not None and not pd.isna(ma20)

    if ma50_valid and ma200_valid:
        if close > ma50 > ma200:
            trend = "Uptrend"
        elif close < ma50 < ma200:
            trend = "Downtrend"
        else:
            trend = "Mixed / Sideways"
    elif ma20_valid and ma50_valid:
        if close > ma20 > ma50:
            trend = "Uptrend"
        elif close < ma20 < ma50:
            trend = "Downtrend"
        else:
            trend = "Mixed / Sideways"
    else:
        trend = "Not enough history yet"

    return {
        "close": round(close, 2),
        "day_change_pct": day_change_pct,
        "rsi": round(float(rsi_today), 1) if not pd.isna(rsi_today) else None,
        "ma20": round(float(ma20), 2) if ma20_valid else None,
        "ma50": round(float(ma50), 2) if ma50_valid else None,
        "ma200": round(float(ma200), 2) if ma200_valid else None,
        "volume_vs_20d_avg": vol_ratio,
        "week_range_high": week_high,
        "week_range_low": week_low,
        "trend": trend,
    }


def explain_no_setup(style: str, df: pd.DataFrame, snapshot: dict) -> str:
    """One-line reason a given timeframe's rule engine did NOT fire, so a
    search never just shows a blank - it says why."""
    rsi_today = snapshot.get("rsi")
    if style == "Intraday (watch)":
        support, resistance = recent_support_resistance(df, SR_LOOKBACK)
        close = df["Close"].iloc[-1]
        if close <= support:
            return f"Trading at/below its {SR_LOOKBACK}-day support (\u20b9{round(support, 2)}) - no breakout setup right now."
        return "Not close enough to its recent resistance to flag as a breakout watch candidate."
    if style == "Swing":
        if rsi_today is not None and rsi_today >= RSI_OVERBOUGHT:
            return f"RSI is {rsi_today} - already overbought, so no fresh entry is flagged here."
        if rsi_today is not None and rsi_today <= RSI_OVERSOLD:
            return f"RSI is {rsi_today} - weak momentum, no confirmed uptrend to enter yet."
        if snapshot.get("ma20") is not None and snapshot.get("ma50") is not None and snapshot["ma20"] < snapshot["ma50"]:
            return f"{SWING_FAST_MA}-day MA is below the {SWING_SLOW_MA}-day MA - short-term trend isn't bullish yet."
        return "No confirmed short-term uptrend crossover right now."
    if style == "Positional":
        if snapshot.get("ma50") is not None and snapshot.get("ma200") is not None and snapshot["ma50"] < snapshot["ma200"]:
            return f"{POSITIONAL_FAST_MA}-day MA is below the {POSITIONAL_SLOW_MA}-day MA - no confirmed long-term uptrend."
        return "Price isn't currently holding above its key long-term moving averages."
    return "No setup detected for this timeframe right now."


def analyze_single_stock(query: str) -> dict:
    """
    Full pipeline for a free-text stock search: resolve a symbol, fetch
    fresh history, run all three rule engines against it (Intraday / Swing
    ("Short Term") / Positional ("Long Term")), and always return a status
    snapshot plus a verdict per timeframe - even timeframes with no setup
    right now come back with a plain-English reason instead of nothing.

    Does NOT fetch live price or news - callers (e.g. the web app) do that
    separately so this function stays testable/offline-friendly.
    """
    candidates = find_symbol_candidates(query)
    if len(candidates) > 1:
        return {"status": "multiple_matches", "query": query, "candidates": candidates}

    symbol = candidates[0]["symbol"] if candidates else normalize_manual_symbol(query)

    # Only need enough rows for a day-change and a support/resistance window
    # to make sense - a recently-listed IPO (which can have well under 30
    # trading days of history) is still a real, searchable stock, just with
    # some indicators (50/200-day MAs, RSI) unavailable until it trades longer.
    MIN_SEARCH_HISTORY_ROWS = 3

    df = fetch_history(symbol, days=SEARCH_HISTORY_DAYS)
    if df is None or len(df) < MIN_SEARCH_HISTORY_ROWS:
        # If we guessed NSE (.NS) and it didn't resolve, try BSE (.BO) before
        # giving up - some stocks are thinly traded or listed on one exchange.
        if symbol.endswith(".NS"):
            alt_symbol = symbol[:-3] + ".BO"
            alt_df = fetch_history(alt_symbol, days=SEARCH_HISTORY_DAYS)
            if alt_df is not None and len(alt_df) >= MIN_SEARCH_HISTORY_ROWS:
                symbol, df = alt_symbol, alt_df
        if df is None or len(df) < MIN_SEARCH_HISTORY_ROWS:
            return {"status": "not_found", "query": query}

    limited_history_note = None
    if len(df) < 30:
        limited_history_note = (
            f"Only {len(df)} trading day(s) of history available (recently listed) - "
            f"longer-term indicators (50/200-day averages, RSI) will fill in as it trades longer."
        )

    snapshot = build_snapshot(df)

    style_fns = [
        ("Intraday (watch)", analyze_intraday_watch),
        ("Swing", analyze_swing),
        ("Positional", analyze_positional),
    ]
    timeframes = []
    for style, fn in style_fns:
        try:
            signal = fn(symbol, df)
        except Exception:
            signal = None
        if signal:
            signal.pop("rank_score", None)
            signal["has_setup"] = True
            timeframes.append(signal)
        else:
            timeframes.append({
                "symbol": symbol, "style": style,
                "direction": "No setup", "has_setup": False,
                "entry": None, "stoploss": None, "target": None, "risk_reward": None,
                "reason": explain_no_setup(style, df, snapshot),
            })

    clean_symbol = symbol.replace(".NS", "").replace(".BO", "")
    return {
        "status": "ok",
        "symbol": symbol,
        "symbol_short": clean_symbol,
        "name": COMPANY_NAMES.get(symbol, clean_symbol),
        "exchange": "BSE" if symbol.endswith(".BO") else "NSE",
        "snapshot": snapshot,
        "timeframes": timeframes,
        "limited_history_note": limited_history_note,
    }


# =====================================================================
# NEWS FETCHER - free, no API key (Google News RSS search feed)
# =====================================================================

def fetch_news(symbol: str, max_items: int = NEWS_PER_STOCK) -> list:
    """
    Fetch recent headlines for one stock using Google News' public RSS search
    feed (no API key needed). Returns a list of {title, link, published}
    dicts, or an empty list if nothing found or the request fails.
    """
    company_name = COMPANY_NAMES.get(symbol, symbol.replace(".NS", ""))
    query = urllib.parse.quote(f"{company_name} stock")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        resp = requests.get(url, timeout=NEWS_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_items]
        headlines = []
        for item in items:
            headlines.append({
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "published": item.findtext("pubDate") or "",
            })
        return headlines
    except Exception as e:
        print(f"  [!] News fetch failed for {symbol}: {e}")
        return []


# =====================================================================
# MARKET & WORLD NEWS DIGEST - same free Google News RSS technique as
# fetch_news, just with broad market/economy queries instead of a stock
# symbol, plus a simple keyword-based "what this could mean" note. This is
# NOT real analysis - it's a fixed set of if-this-keyword-then-this-note
# rules, kept deliberately plain so it's honest about what it is.
# =====================================================================

def _fetch_news_rss(query: str, max_items: int = 5) -> list:
    """Shared Google News RSS fetch for an arbitrary free-text query (not
    tied to a stock symbol, unlike fetch_news above)."""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        resp = requests.get(url, timeout=NEWS_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_items]
        return [
            {
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "published": item.findtext("pubDate") or "",
            }
            for item in items
        ]
    except Exception as e:
        print(f"  [!] Market news fetch failed for query '{query}': {e}")
        return []


# Each rule is (groups, note): the headline matches only if EVERY group has
# at least one phrase present *somewhere* in it - not necessarily adjacent.
# This matters a lot in practice: a real headline like "Fed signals possible
# rate cut next quarter" would NOT match a rigid phrase like "fed rate cut"
# (the words aren't adjacent), but DOES match group ["fed"] AND group
# ["rate cut", ...] independently. Checked in order - first full match wins.
FED_WORDS = ["fed", "federal reserve", "us central bank"]
RBI_WORDS = ["rbi", "reserve bank of india"]
HIKE_VERBS = ["hike", "hikes", "hiking", "raise", "raises", "raising", "increase", "increases", "increasing"]
CUT_VERBS = ["cut", "cuts", "cutting", "lower", "lowers", "lowering", "decrease", "decreases", "decreasing"]
RATE_NOUNS = ["rate", "rates"]
HOLD_WORDS = ["hold", "holds", "unchanged", "steady", "no change", "pause", "paused", "keeps"]

MARKET_IMPACT_RULES = [
    # Entity + action-verb + "rate(s)" checked as independent word groups
    # (not one rigid phrase) - a real headline like "Fed raises interest
    # rates" has "interest" sitting between the verb and noun, so phrase
    # matching misses it; word-level AND matching doesn't.
    ([FED_WORDS, CUT_VERBS, RATE_NOUNS],
     "Lower US interest rates often push global investors toward riskier markets like India, which can support Indian stocks."),
    ([FED_WORDS, HIKE_VERBS, RATE_NOUNS],
     "Rising US interest rates often pull foreign money out of emerging markets like India, which can pressure Indian stocks lower."),
    ([FED_WORDS, HOLD_WORDS],
     "No change in US rates often means stability for now - markets may still react to the Fed's commentary on the outlook."),
    ([RBI_WORDS, CUT_VERBS, RATE_NOUNS],
     "A lower RBI repo rate makes borrowing cheaper, which is usually supportive for company earnings and stock prices."),
    ([RBI_WORDS, HIKE_VERBS, RATE_NOUNS],
     "A higher RBI repo rate makes borrowing costlier for companies and consumers, which can slow growth and weigh on stock prices."),
    ([RBI_WORDS, HOLD_WORDS],
     "No change in RBI policy often means stability for now - markets may still react to the RBI's commentary on growth and inflation."),
    ([["crude oil", "oil price", "oil prices"]],
     "India imports most of its oil - higher crude prices raise costs and inflation (often weighing on the market); lower crude prices usually help."),
    ([["inflation"]],
     "Higher inflation erodes purchasing power and often leads to tighter monetary policy - both tend to weigh on stocks."),
    ([["gdp growth", "economic growth", "gdp data", "gdp forecast"]],
     "Stronger-than-expected economic growth is generally seen as positive for corporate earnings and stock prices."),
    ([["recession"]],
     "Recession fears typically weigh on stock markets, since investors expect weaker corporate earnings ahead."),
    ([["war", "conflict", "tension", "geopolitical", "attack"]],
     "Geopolitical tension tends to increase uncertainty, which often triggers short-term market volatility and a shift to safer assets."),
    ([["rupee", "dollar index"]],
     "A weaker rupee makes imports costlier and can pressure companies reliant on imported inputs, while it tends to help exporters."),
    ([["fii", "foreign investors", "foreign outflow", "foreign inflow"]],
     "Foreign investor flows are one of the biggest short-term drivers of Nifty/Sensex direction - inflows tend to push the market up, outflows down."),
    ([["ipo", "listing debut", "stock market debut"]],
     "A notable new listing can shift investor attention (and money) toward fresh opportunities, sometimes at the expense of existing holdings."),
    ([["earnings", "profit rises", "profit falls", "quarterly results", "q1 results", "q2 results", "q3 results", "q4 results"]],
     "Company earnings directly move their stock price - results beating expectations typically push the price up, missing expectations typically pulls it down."),
]
DEFAULT_MARKET_IMPACT_NOTE = (
    "No specific pattern matched for this one - it's a developing story, worth watching how the "
    "broader market reacts over the next few sessions rather than assuming a direction."
)


def classify_market_impact(headline: str) -> str:
    """Very simple keyword matching - not real analysis, just a quick,
    honest, plain-English starting point."""
    headline_lower = headline.lower()
    for groups, note in MARKET_IMPACT_RULES:
        if all(any(kw in headline_lower for kw in group) for group in groups):
            return note
    return DEFAULT_MARKET_IMPACT_NOTE


def _collect_news(queries: list, max_items: int) -> list:
    seen_titles = set()
    out = []
    for q in queries:
        for item in _fetch_news_rss(q, max_items=4):
            title = item["title"]
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            out.append({
                "title": title,
                "link": item["link"],
                "published": item["published"],
                "impact": classify_market_impact(title),
            })
        time.sleep(0.3)  # be gentle on the free RSS feed, same as fetch_news
        if len(out) >= max_items:
            break
    return out[:max_items]


def fetch_latest_market_news(max_items: int = 5) -> list:
    """Indian market news - Sensex/Nifty, RBI, FIIs."""
    queries = [
        "Sensex Nifty stock market today",
        "RBI monetary policy repo rate",
        "FII foreign investors India stock market",
    ]
    return _collect_news(queries, max_items)


def fetch_latest_world_news(max_items: int = 5) -> list:
    """World/macro news that commonly moves Indian markets."""
    queries = [
        "US Federal Reserve interest rate",
        "crude oil price today",
        "global stock markets today",
    ]
    return _collect_news(queries, max_items)


def get_latest_news_digest() -> dict:
    return {
        "market": fetch_latest_market_news(),
        "world": fetch_latest_world_news(),
    }


# =====================================================================
# IPO GMP TRACKER
#
# Honest note on this section: there is no official, stable API for grey
# market premium data - it's an informal, unregulated figure that lives on
# a handful of tracking websites, and those sites' HTML structure can change
# without notice, which would silently break an auto-scraper with no way for
# you to know it broke.
#
# So this uses a simple CSV file (ipo_watchlist.csv) that YOU update each
# morning by checking any GMP site you trust (takes ~1-2 minutes) - and the
# script does the classification math for you (estimated listing price,
# gain %, Strong/Moderate/Weak/Avoid verdict). This is slightly more manual
# but will never silently give you wrong numbers from a broken scraper.
# =====================================================================

IPO_CSV_TEMPLATE_HEADER = "name,segment,issue_price,gmp,open_date,close_date,listing_date\n"


def ensure_ipo_template_exists(path: str = IPO_WATCHLIST_FILE):
    """Creates an empty template file with instructions if one doesn't exist yet."""
    if os.path.exists(path):
        return
    with open(path, "w") as f:
        f.write(IPO_CSV_TEMPLATE_HEADER)
        f.write("# Example row (delete the # and fill in real IPOs you're tracking):\n")
        f.write("# Highness Micro,SME,120,29,2026-09-09,2026-09-11,2026-09-16\n")
    print(f"Created empty {path} - add today's IPOs to it, then re-run for IPO analysis.")


try:
    from nse import NSE
except ImportError:
    NSE = None

NSE_IPO_CACHE_DIR = "nse_ipo_cache"


def _parse_nse_ipo_date(s: str) -> str:
    """NSE gives dates like '26-Mar-2024' - we show '26 Mar' (year is implied by context)."""
    if not s:
        return ""
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").strftime("%d %b")
    except ValueError:
        return s


def _normalize_nse_ipo(raw: dict) -> dict:
    """Converts one raw NSE IPO record (whatever shape listCurrentIPO /
    listUpcomingIPO / listPastIPO return) into our unified shape. Uses
    .get() everywhere and never raises - a field NSE renamed or omitted
    just shows up blank rather than breaking the whole IPO tab."""
    symbol = raw.get("symbol") or ""
    series = (raw.get("series") or "").upper()

    issue_price_raw = raw.get("issuePrice") or ""
    price_numbers = re.findall(r"[\d,]+\.?\d*", issue_price_raw)
    issue_price_upper = None
    if price_numbers:
        try:
            issue_price_upper = float(price_numbers[-1].replace(",", ""))
        except ValueError:
            issue_price_upper = None

    sub_raw = raw.get("noOfTime")
    subscription_times = None
    if sub_raw not in (None, ""):
        try:
            subscription_times = round(float(sub_raw), 2)
        except (ValueError, TypeError):
            subscription_times = None

    return {
        "symbol": symbol,
        "name": raw.get("companyName") or symbol or "Unknown",
        "segment": "SME" if series in ("SM", "ST") else "Mainboard",
        "issue_price_range": issue_price_raw,
        "issue_price": issue_price_upper,
        "open_date": _parse_nse_ipo_date(raw.get("issueStartDate", "")),
        "close_date": _parse_nse_ipo_date(raw.get("issueEndDate", "")),
        "status": raw.get("status") or "",
        "issue_size": raw.get("issueSize") or "",
        "subscription_times": subscription_times,
    }


def fetch_nse_ipo_raw() -> dict:
    """
    Live IPO calendar straight from NSE's own (unofficial) data - dates,
    price band, lot/issue size, subscription multiple. This is real
    exchange data, not GMP (which has no free/official source at all -
    see the GMP override system in app.py).
    """
    if NSE is None:
        return {"current": [], "upcoming": [], "recent": []}
    try:
        os.makedirs(NSE_IPO_CACHE_DIR, exist_ok=True)
        with NSE(download_folder=NSE_IPO_CACHE_DIR) as nse:
            current = nse.listCurrentIPO() or []
            upcoming = nse.listUpcomingIPO() or []
            recent = nse.listPastIPO() or []
    except Exception as e:
        print(f"  [!] NSE IPO fetch failed: {e}")
        return {"current": [], "upcoming": [], "recent": []}
    return {
        "current": [_normalize_nse_ipo(r) for r in current],
        "upcoming": [_normalize_nse_ipo(r) for r in upcoming],
        "recent": [_normalize_nse_ipo(r) for r in recent],
    }


def build_ipo_dashboard(gmp_overrides: dict) -> dict:
    """
    Merges the live NSE IPO calendar with manually-entered GMP values
    (gmp_overrides: {symbol: gmp_float}) and computes the same
    Strong/Moderate/Weak/Avoid verdict used elsewhere - only for entries
    where a GMP has actually been entered. Entries without a GMP yet are
    still shown (dates, price band, subscription), just without a verdict.
    """
    raw = fetch_nse_ipo_raw()

    def merge_section(entries):
        out = []
        for ipo in entries:
            ipo = dict(ipo)
            gmp = gmp_overrides.get(ipo["symbol"])
            if gmp is not None and ipo["issue_price"]:
                est_listing, pct, verdict = classify_gmp(ipo["issue_price"], gmp)
                ipo["gmp"] = gmp
                ipo["est_listing"] = est_listing
                ipo["gain_pct"] = pct
                ipo["verdict"] = verdict
            else:
                ipo["gmp"] = None
                ipo["est_listing"] = None
                ipo["gain_pct"] = None
                ipo["verdict"] = "No GMP entered yet"
            out.append(ipo)
        return out

    return {
        "current": merge_section(raw["current"]),
        "upcoming": merge_section(raw["upcoming"]),
        "recent": merge_section(raw["recent"]),
    }


# =====================================================================
# DIVIDEND PICKS - upcoming ex-dividend dates (live, from NSE's own
# corporate-actions feed) crossed with the same Entry/Stoploss/Target rule
# engines used everywhere else. Restricted to our existing watchlist so
# this stays fast and blue-chip-focused, and can run on its own schedule
# without needing the full 94-stock "Generate report" scan.
# =====================================================================

DIVIDEND_MIN_DAYS_AHEAD = 14   # the whole point: enough notice to actually act
DIVIDEND_MAX_DAYS_AHEAD = 90   # don't clutter the list with things months out


def _parse_nse_date_full(s: str):
    """Full calendar date (unlike _parse_nse_ipo_date, which drops the year
    for display) - needed here to compute days-until-ex-date."""
    if not s or s == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_dividend_amount(subject: str):
    """NSE gives the amount embedded in free text like 'Interim Dividend -
    Rs 5.50 Per Share' - there's no separate numeric field."""
    if not subject:
        return None
    m = re.search(r"Rs\.?\s*([\d,]+\.?\d*)", subject, re.IGNORECASE)
    if not m:
        m = re.search(r"₹\s*([\d,]+\.?\d*)", subject)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def fetch_nse_dividend_actions(
    min_days_ahead: int = DIVIDEND_MIN_DAYS_AHEAD,
    max_days_ahead: int = DIVIDEND_MAX_DAYS_AHEAD,
) -> list:
    """
    Forthcoming dividend announcements from NSE's own (unofficial)
    corporate-actions feed, filtered to ex-dates between min_days_ahead and
    max_days_ahead from today. Same NSE-access caveats as the IPO calendar.
    """
    if NSE is None:
        return []
    today = date.today()
    from_dt = datetime.combine(today, datetime.min.time())
    to_dt = datetime.combine(today + timedelta(days=max_days_ahead + 1), datetime.min.time())
    try:
        os.makedirs(NSE_IPO_CACHE_DIR, exist_ok=True)
        with NSE(download_folder=NSE_IPO_CACHE_DIR) as nse:
            raw = nse.actions(segment="equities", from_dt=from_dt, to_dt=to_dt) or []
    except Exception as e:
        print(f"  [!] NSE corporate actions fetch failed: {e}")
        return []

    out = []
    for item in raw:
        subject = item.get("subject") or item.get("purpose") or ""
        if "dividend" not in subject.lower():
            continue
        ex_date = _parse_nse_date_full(
            item.get("exDate") or item.get("ex_date") or item.get("ex-date") or ""
        )
        if ex_date is None:
            continue
        days_away = (ex_date - today).days
        if days_away < min_days_ahead or days_away > max_days_ahead:
            continue
        symbol = item.get("symbol") or ""
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": item.get("comp") or item.get("company") or symbol,
            "subject": subject,
            "ex_date": ex_date.strftime("%d %b %Y"),
            "days_to_ex_date": days_away,
            "dividend_amount": _parse_dividend_amount(subject),
        })
    return out


def get_dividend_picks(
    min_days_ahead: int = DIVIDEND_MIN_DAYS_AHEAD,
    max_days_ahead: int = DIVIDEND_MAX_DAYS_AHEAD,
) -> list:
    """
    For every watchlist stock going ex-dividend within the window, pairs it
    with a technical setup (Swing preferred, falling back to Positional)
    from the exact same rule engines used in the daily scan - so this
    always shows real, freshly-computed Entry/Stoploss/Target levels, not
    a copy of whatever the last "Generate report" happened to find.
    """
    actions = fetch_nse_dividend_actions(min_days_ahead, max_days_ahead)
    if not actions:
        return []

    watchlist_bases = {s.replace(".NS", "") for s in WATCHLIST}
    picks = []
    for a in actions:
        if a["symbol"] not in watchlist_bases:
            continue
        symbol = a["symbol"] + ".NS"
        df = fetch_history(symbol, days=SEARCH_HISTORY_DAYS)
        if df is None or len(df) < 30:
            continue
        current_price = round(float(df["Close"].iloc[-1]), 2)
        div_yield = (
            round(a["dividend_amount"] / current_price * 100, 2)
            if a["dividend_amount"] else None
        )

        signal, style = None, None
        for candidate_style, fn in [("Swing", analyze_swing), ("Positional", analyze_positional)]:
            try:
                sig = fn(symbol, df)
            except Exception:
                sig = None
            if sig:
                signal, style = sig, candidate_style
                break

        amount_str = f"₹{a['dividend_amount']}/share" if a["dividend_amount"] is not None else "an undisclosed amount"
        pick = {
            "symbol": symbol,
            "symbol_short": a["symbol"],
            "name": a["name"],
            "ex_date": a["ex_date"],
            "days_to_ex_date": a["days_to_ex_date"],
            "dividend_amount": a["dividend_amount"],
            "dividend_yield_pct": div_yield,
            "current_price": current_price,
            "has_setup": signal is not None,
        }
        if signal:
            pick["style"] = style
            pick["entry"] = signal["entry"]
            pick["stoploss"] = signal["stoploss"]
            pick["target"] = signal["target"]
            pick["risk_reward"] = signal["risk_reward"]
            pick["reason"] = (
                f"Goes ex-dividend on {a['ex_date']} ({a['days_to_ex_date']} days away), "
                f"paying {amount_str}. {signal['reason']}"
            )
        else:
            pick["reason"] = (
                f"Goes ex-dividend on {a['ex_date']} ({a['days_to_ex_date']} days away), "
                f"paying {amount_str} - but no confirmed bullish technical setup right now, "
                f"so the dividend alone isn't shown as a reason to enter."
            )
        picks.append(pick)

    picks.sort(key=lambda p: p["days_to_ex_date"])
    return picks



# =====================================================================
# QUARTERLY RESULTS CALENDAR - results already filed this month, plus
# board meetings scheduled to consider results in the coming weeks. Source
# is NSE's own (unofficial) corporate-filings feed, so it covers NSE-listed
# companies; BSE-only companies are not included. Same NSE-access caveats
# as the IPO calendar: NSE can block or throttle automated requests, in
# which case this returns an "error" message instead of crashing.
# =====================================================================
RESULTS_UPCOMING_DAYS_AHEAD = 30
RESULTS_MAX_ROWS = 1500


def _nse_api_get(path: str, params: dict):
    sess = requests.Session()
    sess.headers.update(dict(_BROWSER_HEADERS, Referer="https://www.nseindia.com/"))
    sess.get("https://www.nseindia.com", timeout=15)       # primes the cookies NSE requires
    resp = sess.get("https://www.nseindia.com/api/" + path, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_any_date(value):
    """Handles '09-Oct-2026', '09-Oct-2026 16:05:11', '2026-10-09', etc."""
    if not value:
        return None
    text = str(value).strip()
    for fmt, width in (("%d-%b-%Y", 11), ("%Y-%m-%d", 10), ("%d-%m-%Y", 10), ("%d-%b-%y", 9)):
        try:
            return datetime.strptime(text[:width].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _first(item: dict, *keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _quarter_label(quarter_end: date) -> str:
    q = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(quarter_end.month)
    if not q:
        return ""
    fy = quarter_end.year + 1 if quarter_end.month >= 4 else quarter_end.year
    return f"{q} FY{str(fy)[-2:]}"


def _latest_quarter_end_before(d: date) -> date:
    ends = [date(y, m, dd) for y in (d.year - 1, d.year) for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(e for e in ends if e < d)


def _results_row(symbol, name, d, quarter, kind, link):
    return {
        "symbol": symbol,
        "name": name or symbol,
        "date": d.isoformat(),
        "date_display": d.strftime("%d %b %Y"),
        "weekday": d.strftime("%a"),
        "quarter": quarter,
        "kind": kind or "",
        "link": link or "",
        "in_nifty100": f"{symbol}.NS" in WATCHLIST,
    }


def get_results_calendar() -> dict:
    today = date.today()
    month_start = today.replace(day=1)
    horizon = today + timedelta(days=RESULTS_UPCOMING_DAYS_AHEAD)
    fmt = "%d-%m-%Y"
    errors = []

    published, seen_pub = [], set()
    try:
        raw = _nse_api_get("corporates-financial-results", {
            "index": "equities", "period": "Quarterly",
            "from_date": month_start.strftime(fmt), "to_date": today.strftime(fmt),
        })
        raw = raw if isinstance(raw, list) else raw.get("data", [])
        for it in raw:
            symbol = _first(it, "symbol")
            d = _parse_any_date(_first(it, "broadcastDate", "filingDate", "broadCastDate"))
            if not symbol or d is None or d < month_start or d > today:
                continue
            qend = _parse_any_date(_first(it, "toDate", "qe_Date", "quarterEnded"))
            quarter = _quarter_label(qend) if qend else _quarter_label(_latest_quarter_end_before(d))
            key = (symbol, quarter)
            if key in seen_pub:
                continue
            seen_pub.add(key)
            published.append(_results_row(
                symbol, _first(it, "companyName", "comp"), d, quarter,
                _first(it, "consolidated", "resultsType"), _first(it, "xbrl", "attachment", "reAttachement")))
    except Exception as e:
        errors.append(f"published results: {e}")

    upcoming = []
    try:
        raw = _nse_api_get("corporate-board-meetings", {
            "index": "equities",
            "from_date": month_start.strftime(fmt), "to_date": horizon.strftime(fmt),
        })
        raw = raw if isinstance(raw, list) else raw.get("data", [])
        seen_up = set()
        for it in raw:
            purpose = f"{_first(it, 'purpose', 'bm_purpose') or ''} {_first(it, 'details', 'bm_desc', 'description') or ''}"
            if "result" not in purpose.lower():
                continue
            symbol = _first(it, "symbol", "bm_symbol")
            d = _parse_any_date(_first(it, "meetingdate", "bm_date", "date"))
            if not symbol or d is None:
                continue
            quarter = _quarter_label(_latest_quarter_end_before(d))
            row = _results_row(symbol, _first(it, "companyName", "sm_name", "comp"), d, quarter,
                               "Board meeting", _first(it, "attachment"))
            if d >= today:
                if (symbol, quarter) in seen_pub or (symbol, d) in seen_up:
                    continue                      # already filed / duplicate row
                seen_up.add((symbol, d))
                upcoming.append(row)
            elif not published:
                published.append(dict(row, kind="Board meeting held"))   # fallback if the filings feed was empty
    except Exception as e:
        errors.append(f"upcoming board meetings: {e}")

    published.sort(key=lambda r: (r["date"], not r["in_nifty100"]), reverse=True)
    upcoming.sort(key=lambda r: (r["date"], not r["in_nifty100"], r["name"]))
    return {
        "month_label": today.strftime("%B %Y"),
        "published": published[:RESULTS_MAX_ROWS],
        "upcoming": upcoming[:RESULTS_MAX_ROWS],
        "error": "; ".join(errors) if errors else None,
    }



# =====================================================================
# RESULTS-STOCK SUGGESTION - a plain rule-based "is this a good buy right
# now?" read for one stock in the Quarterly Results list. It reuses the
# same Short Term (Swing) and Long Term (Positional) rule engines as the
# daily scan, adds a headline-keyword news check and a results-date
# caution. It is NOT a forecast and does not read the actual results.
# =====================================================================
RESULT_EVENT_RISK_DAYS = 7          # results this close -> "Wait", prices often gap on the numbers
BIG_DROP_AFTER_RESULTS_PCT = -5.0   # fell this much since results -> "Wait"
_NEGATIVE_NEWS_WORDS = [
    "fraud", "probe", "raid", "penalty", "fine", "downgrade", "plunge", "slump", "tumbles", "crashes",
    "loss widens", "net loss", "profit falls", "profit drops", "profit declines", "misses estimates",
    "default", "resigns", "sebi order", "lawsuit", "cuts guidance", "weak results",
]
_POSITIVE_NEWS_WORDS = [
    "profit jumps", "profit rises", "profit surges", "net profit up", "beats estimates", "record profit",
    "upgrade", "order win", "wins order", "bags order", "strong results", "dividend", "buyback", "surges",
]


def _news_tone(headlines: list):
    titles = [h.get("title", "") for h in headlines]
    neg = [t for t in titles if any(w in t.lower() for w in _NEGATIVE_NEWS_WORDS)]
    pos = [t for t in titles if any(w in t.lower() for w in _POSITIVE_NEWS_WORDS)]
    if neg and pos:
        return "mixed", neg
    if neg:
        return "negative", neg
    if pos:
        return "positive", []
    return "neutral", []


def get_result_suggestion(symbol: str, result_date: str = None, status: str = "upcoming") -> dict:
    sym = symbol if symbol.endswith((".NS", ".BO")) else symbol + ".NS"
    out = {"symbol": symbol, "verdict": "No data", "verdict_class": "neutral",
           "summary": "Not enough price history to analyse this stock.", "notes": [],
           "setups": [], "news": [], "news_tone": "neutral", "snapshot": None}
    df = fetch_history(sym, days=SEARCH_HISTORY_DAYS)
    if df is None or len(df) < 30:
        return out

    snap = build_snapshot(df)
    out["snapshot"] = {k: snap[k] for k in ("close", "day_change_pct", "rsi", "trend")}

    for label, fn in (("Short Term (1-3 weeks)", analyze_swing), ("Long Term (2-6 months)", analyze_positional)):
        try:
            sig = fn(sym, df)
        except Exception:
            sig = None
        if sig:
            out["setups"].append({k: sig[k] for k in ("entry", "stoploss", "target", "risk_reward")} | {"label": label})

    news = fetch_news(sym, max_items=3)
    out["news"] = [{"title": n["title"], "link": n["link"]} for n in news if n.get("title")]
    tone, neg_titles = _news_tone(news)
    out["news_tone"] = tone

    days_to_result = None
    reaction_pct = None
    if result_date:
        try:
            rd = date.fromisoformat(result_date)
            if status == "upcoming":
                days_to_result = (rd - date.today()).days
            else:
                idx_dates = [i.date() if hasattr(i, "date") else i for i in df.index]
                after = [k for k, dd in enumerate(idx_dates) if dd >= rd]
                if after and after[0] < len(df) - 1:
                    base = float(df["Close"].iloc[after[0]])
                    reaction_pct = round((float(df["Close"].iloc[-1]) - base) / base * 100, 1)
        except Exception:
            pass

    notes = []
    if tone in ("negative", "mixed"):
        notes.append("Recent headlines contain negative wording - read them before acting.")
    if days_to_result is not None and 0 <= days_to_result <= RESULT_EVENT_RISK_DAYS:
        when = "today" if days_to_result == 0 else f"in {days_to_result} day(s)"
        notes.append(f"Results are due {when}; prices often gap sharply either way on the numbers.")
    if reaction_pct is not None:
        notes.append(f"Price is {reaction_pct:+.1f}% since the results date (close to close).")

    if not out["setups"]:
        out.update(verdict="Not now", verdict_class="avoid",
                   summary=f"No buy setup right now - trend is {snap['trend'].lower()}"
                           + (f", RSI {snap['rsi']}." if snap["rsi"] is not None else "."))
    elif tone == "negative" or (days_to_result is not None and 0 <= days_to_result <= RESULT_EVENT_RISK_DAYS) \
            or (reaction_pct is not None and reaction_pct <= BIG_DROP_AFTER_RESULTS_PCT):
        out.update(verdict="Wait", verdict_class="wait",
                   summary="The chart shows a buy setup, but the points below argue for waiting first. "
                           "Levels are shown only as a reference.")
    else:
        out.update(verdict="Good to buy", verdict_class="buy",
                   summary=f"Chart setup is valid (trend: {snap['trend'].lower()}) and nothing negative stands out in the news.")
    out["notes"] = notes
    return out



# =====================================================================
# FUNDAMENTALLY STRONG STOCKS - a simple quality screen on Yahoo Finance
# fundamentals (ROE, debt, growth, margins, valuation, size), then an
# Entry / Stop-loss / Target / timeline plan for each stock that passes.
# Fundamentals only change when quarterly results are filed, so they are
# cached on disk for FUNDAMENTALS_TTL_DAYS; prices/levels are recomputed
# whenever the picks are rebuilt. This is a screening aid, not research:
# Yahoo's Indian fundamentals can be missing or stale, and a screen cannot
# see governance, promoter pledging, order books or sector cycles.
# =====================================================================
FUNDAMENTALS_CACHE_FILE = "fundamentals_cache.json"
FUNDAMENTALS_TTL_DAYS = 7
FUNDAMENTAL_CANDIDATE_LIMIT = 300      # most-liquid scanned stocks examined
FUNDAMENTAL_MIN_SCORE_PCT = 0.70       # share of available points needed to count as "strong"
FUNDAMENTAL_MAX_PICKS = 60
MIN_MARKET_CAP_INR = 5e10              # Rs 5,000 crore


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def fetch_fundamentals(sym: str):
    info = yf.Ticker(sym).info or {}
    if info.get("marketCap") is None and info.get("returnOnEquity") is None:
        return None
    de = _num(info.get("debtToEquity"))           # Yahoo reports this as a percentage (45 = 0.45x)
    return {
        "name": info.get("longName") or info.get("shortName") or sym,
        "sector": info.get("sector") or "",
        "roe": _num(info.get("returnOnEquity")),
        "debt_to_equity": de / 100 if de is not None else None,
        "revenue_growth": _num(info.get("revenueGrowth")),
        "earnings_growth": _num(info.get("earningsGrowth")),
        "profit_margin": _num(info.get("profitMargins")),
        "current_ratio": _num(info.get("currentRatio")),
        "pe": _num(info.get("trailingPE")),
        "market_cap": _num(info.get("marketCap")),
    }


def score_fundamentals(f: dict) -> dict:
    """Points-based quality score. Banks/NBFCs are not penalised on debt or current ratio (not meaningful for them)."""
    financial = "financial" in (f.get("sector") or "").lower()
    score, max_score, present, tags = 0, 0, 0, []

    def higher_better(val, tiers, tag):
        nonlocal score, max_score, present
        max_score += tiers[0][1]
        if val is None:
            return
        present += 1
        for threshold, pts in tiers:
            if val >= threshold:
                score += pts
                tags.append(tag(val))
                return

    roe, de = f.get("roe"), f.get("debt_to_equity")
    higher_better(roe, [(0.15, 2), (0.12, 1)], lambda v: f"ROE {v * 100:.0f}%")
    if not financial:
        max_score += 2
        if de is not None:
            present += 1
            if de <= 0.5:
                score += 2
                tags.append(f"Low debt (D/E {de:.2f})")
            elif de <= 1.0:
                score += 1
                tags.append(f"Moderate debt (D/E {de:.2f})")
    higher_better(f.get("revenue_growth"), [(0.10, 2), (0.05, 1)], lambda v: f"Revenue {v * 100:+.0f}% YoY")
    higher_better(f.get("earnings_growth"), [(0.10, 2), (0.0001, 1)], lambda v: f"Profit {v * 100:+.0f}% YoY")
    higher_better(f.get("profit_margin"), [(0.10, 1)], lambda v: f"Net margin {v * 100:.0f}%")
    if not financial:
        higher_better(f.get("current_ratio"), [(1.2, 1)], lambda v: f"Current ratio {v:.1f}")
    pe = f.get("pe")
    max_score += 1
    if pe is not None:
        present += 1
        if 0 < pe <= 50:
            score += 1
            tags.append(f"P/E {pe:.0f}")
    higher_better(f.get("market_cap"), [(MIN_MARKET_CAP_INR, 1)], lambda v: "Large/mid cap")

    pct = score / max_score if max_score else 0
    eg = f.get("earnings_growth")
    passed = (present >= 4 and pct >= FUNDAMENTAL_MIN_SCORE_PCT
              and roe is not None and roe >= 0.12 and eg is not None and eg > 0)
    return {"score_pct": round(pct * 100), "passed": passed, "tags": tags}


def _timeline_for(upside_pct: float) -> str:
    """Rule of thumb only: the further the target, the longer it typically takes."""
    if upside_pct <= 10:
        return "1-3 months"
    if upside_pct <= 20:
        return "3-6 months"
    if upside_pct <= 35:
        return "6-12 months"
    return "12+ months"


def plan_fundamental_trade(sym: str, df: pd.DataFrame) -> dict:
    snap = build_snapshot(df)
    close = snap["close"]
    prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else close
    plan = {"price": round(close, 2), "prev_close": round(prev_close, 2),
            "day_change_pct": snap["day_change_pct"], "trend": snap["trend"], "rsi": snap["rsi"],
            "entry": None, "stoploss": None, "target": None, "risk_reward": None,
            "upside_pct": None, "timeline": "", "status": "No levels", "status_class": "wait", "note": ""}

    sig = analyze_positional(sym, df)
    if sig:
        entry, sl, tgt, rr = sig["entry"], sig["stoploss"], sig["target"], sig["risk_reward"]
        plan.update(status="Buy zone", status_class="buy",
                    note="Price is above rising 50/200-day averages, so the chart supports entering now.")
    else:
        ma50 = sma(df["Close"], 50).iloc[-1]
        atr_today = atr(df, 14).iloc[-1]
        if pd.isna(ma50) or pd.isna(atr_today):
            plan["note"] = "Not enough price history to set levels."
            return plan
        entry = round(float(ma50) * 1.01, 2)
        sl = round(entry - float(atr_today) * 2.5, 2)
        if entry <= sl or sl <= 0:
            plan["note"] = "Could not size a sensible stop-loss."
            return plan
        rr = POSITIONAL_TARGET_RR
        tgt = round(entry + (entry - sl) * rr, 2)
        way = "a pullback to" if close > entry else "a recovery above"
        plan.update(status="Wait", status_class="wait",
                    note=f"Quality business, but the chart is {snap['trend'].lower()}. Consider buying only on {way} "
                         f"the 50-day average (about Rs {entry}).")
    upside = (tgt - entry) / entry * 100
    plan.update(entry=entry, stoploss=sl, target=tgt, risk_reward=rr, upside_pct=round(upside, 1),
                timeline=_timeline_for(upside))
    return plan


def _load_fund_cache() -> dict:
    try:
        with open(FUNDAMENTALS_CACHE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def get_fundamental_picks(candidates: list, progress_cb=None) -> list:
    from concurrent.futures import ThreadPoolExecutor
    cache = _load_fund_cache()
    today = date.today()

    def fresh(entry):
        try:
            return (today - date.fromisoformat(entry["fetched_on"])).days < FUNDAMENTALS_TTL_DAYS
        except Exception:
            return False

    todo = [c for c in candidates if not fresh(cache.get(c, {}))]
    done = len(candidates) - len(todo)
    if progress_cb:
        progress_cb(done, len(candidates))

    def work(sym):
        try:
            f = fetch_fundamentals(sym)
        except Exception:
            f = None
        time.sleep(0.05)
        return sym, f

    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, f in pool.map(work, todo):
            if f:                                   # failures aren't cached, so they retry next time
                cache[sym] = {"fetched_on": today.isoformat(), "data": f}
            done += 1
            if progress_cb:
                progress_cb(done, len(candidates))
    try:
        with open(FUNDAMENTALS_CACHE_FILE, "w") as fh:
            json.dump(cache, fh)
    except Exception as e:
        print(f"  [!] Could not save fundamentals cache: {e}")

    strong = []
    for sym in candidates:
        f = (cache.get(sym) or {}).get("data")
        if f:
            sc = score_fundamentals(f)
            if sc["passed"]:
                strong.append((sym, f, sc))
    strong.sort(key=lambda t: t[2]["score_pct"], reverse=True)

    picks = []
    for sym, f, sc in strong[:FUNDAMENTAL_MAX_PICKS]:
        df = fetch_history(sym, days=SEARCH_HISTORY_DAYS)
        if df is None or len(df) < 60:
            continue
        plan = plan_fundamental_trade(sym, df)
        picks.append(dict(plan, **{
            "symbol": sym, "symbol_short": sym.replace(".NS", "").replace(".BO", ""),
            "name": f.get("name") or sym, "sector": f.get("sector") or "",
            "score": sc["score_pct"], "tags": sc["tags"],
            "metrics": {k: f.get(k) for k in ("roe", "debt_to_equity", "revenue_growth", "earnings_growth",
                                              "profit_margin", "pe", "market_cap")},
        }))
    return picks


def classify_gmp(issue_price: float, gmp: float) -> tuple:
    """Returns (estimated_listing_price, gain_pct, verdict_string)."""
    if issue_price <= 0:
        return None, None, "Invalid issue price"
    est_listing = issue_price + gmp
    pct = (gmp / issue_price) * 100
    if pct < 0:
        verdict = "Avoid (negative GMP)"
    elif pct >= IPO_STRONG_THRESHOLD_PCT:
        verdict = "Strong"
    elif pct >= IPO_MODERATE_THRESHOLD_PCT:
        verdict = "Moderate"
    else:
        verdict = "Weak"
    return round(est_listing, 2), round(pct, 1), verdict


def load_ipo_watchlist(path: str = IPO_WATCHLIST_FILE) -> list:
    """Reads the manually-maintained IPO CSV and returns classified entries."""
    ensure_ipo_template_exists(path)
    results = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(row for row in f if not row.strip().startswith("#") and row.strip())
            for row in reader:
                try:
                    issue_price = float(row["issue_price"])
                    gmp = float(row["gmp"])
                except (ValueError, KeyError):
                    continue
                est_listing, pct, verdict = classify_gmp(issue_price, gmp)
                results.append({
                    "name": row.get("name", "Unknown"),
                    "segment": row.get("segment", ""),
                    "issue_price": issue_price,
                    "gmp": gmp,
                    "est_listing": est_listing,
                    "gain_pct": pct,
                    "verdict": verdict,
                    "close_date": row.get("close_date", ""),
                    "listing_date": row.get("listing_date", ""),
                })
    except FileNotFoundError:
        pass
    return results


# =====================================================================
# REPORT GENERATOR
# =====================================================================

def generate_report(all_signals: list, news_by_symbol: dict = None, ipo_list: list = None) -> str:
    news_by_symbol = news_by_symbol or {}
    ipo_list = ipo_list or []
    today = date.today().strftime("%A, %d %B %Y")
    lines = [f"DAILY STOCK BRIEF - {today}", "=" * 50, ""]

    # ---- IPO section (always shown if there's anything in the watchlist) ----
    if ipo_list:
        lines.append(f"--- IPO WATCH ({len(ipo_list)}) ---")
        for ipo in ipo_list:
            lines.append(f"\n{ipo['name']} ({ipo['segment']})")
            lines.append(f"  Issue price:     {ipo['issue_price']}")
            lines.append(f"  Current GMP:     {ipo['gmp']}")
            lines.append(f"  Est. listing:    {ipo['est_listing']}  ({ipo['gain_pct']}%)")
            lines.append(f"  Verdict:         {ipo['verdict']}")
            if ipo["close_date"] or ipo["listing_date"]:
                lines.append(f"  Closes: {ipo['close_date']}  |  Lists: {ipo['listing_date']}")
        lines.append("")
        lines.append("(GMP is an unofficial, informal indicator - directional signal, not a guarantee.)")
        lines.append("")

    if not all_signals:
        lines.append("No stocks matched today's criteria. No forced trades - that's fine.")
        return "\n".join(lines)

    by_style = {}
    for s in all_signals:
        by_style.setdefault(s["style"], []).append(s)

    for style in ["Intraday (watch)", "Swing", "Positional"]:
        signals = by_style.get(style, [])
        if not signals:
            continue
        lines.append(f"--- {style.upper()} ({len(signals)}) ---")
        for s in signals:
            lines.append(f"\n{s['symbol'].replace('.NS', '').replace('.BO', '')}")
            lines.append(f"  Entry:     {s['entry']}")
            lines.append(f"  Stoploss:  {s['stoploss']}")
            lines.append(f"  Target:    {s['target']}  (R:R ~1:{s['risk_reward']})")
            lines.append(f"  Why:       {s['reason']}")
            headlines = news_by_symbol.get(s["symbol"], [])
            if headlines:
                lines.append("  News:")
                for h in headlines:
                    lines.append(f"    - {h['title']}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("Reminder: these are rule-based technical setups, not guarantees.")
    lines.append("You decide whether to act on each one. Always size positions")
    lines.append("so a stoploss hit is a loss you're fully comfortable with.")
    return "\n".join(lines)


def save_report(report_text: str, path: str = "daily_report.txt"):
    with open(path, "w") as f:
        f.write(report_text)
    print(f"Report saved to {path}")


# =====================================================================
# MAIN
# =====================================================================

def main():
    print(f"Fetching data for {len(NIFTY_50)} stocks...")
    price_data = fetch_all(NIFTY_50)
    print(f"Successfully fetched {len(price_data)}/{len(NIFTY_50)} stocks.\n")

    print("Running analysis...")
    all_signals = []
    for symbol, df in price_data.items():
        all_signals.extend(analyze_stock(symbol, df))
    print(f"Found {len(all_signals)} signals.\n")

    print("Fetching news for flagged stocks...")
    news_by_symbol = {}
    flagged_symbols = {s["symbol"] for s in all_signals}
    for symbol in flagged_symbols:
        news_by_symbol[symbol] = fetch_news(symbol)
        time.sleep(0.3)  # be gentle on the free news feed

    print("Loading IPO watchlist...")
    ipo_list = load_ipo_watchlist()

    report = generate_report(all_signals, news_by_symbol, ipo_list)
    print(report)
    save_report(report)


if __name__ == "__main__":
    main()
