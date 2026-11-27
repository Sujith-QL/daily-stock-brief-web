"""
search_job.py - builds data/search.json so the static site can "search" a stock
without a server. Imported by fetch_data.py (full scan only).

For every symbol it downloads ~1 year of daily candles (in batches) and works out the
snapshot the page shows (trend, RSI, 52-week range, volume vs 20-day average) plus
simple Intraday / Swing / Positional entry-stoploss-target levels based on ATR.

NOTE: the setup rules below are a simple, generic approximation. They are NOT the
logic from your old app.py search. If you want the exact old rules, reuse that code
inside analyse().
"""
import math

import pandas as pd
import yfinance as yf

BATCH = 100


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def _atr(df, n=14):
    prev = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - prev).abs(),
                    (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _num(x, nd=2):
    try:
        x = float(x)
        return None if math.isnan(x) else round(x, nd)
    except Exception:
        return None


def _setup(style, ok, close, atr, mult, rr, reason_yes, reason_no, warning=None):
    if not ok or not atr:
        return {"style": style, "has_setup": False, "reason": reason_no}
    stop = close - mult * atr
    target = close + rr * mult * atr
    return {
        "style": style, "has_setup": True,
        "entry": _num(close), "stoploss": _num(stop), "target": _num(target),
        "risk_reward": rr, "reason": reason_yes,
        "warning": warning,
    }


def analyse(sym, df, name=None):
    df = df.dropna(subset=["Close"])
    if len(df) < 60:
        return None
    close = df["Close"]
    c = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")
    rsi = _num(_rsi(close).iloc[-1], 1)
    atr = float(_atr(df).iloc[-1])
    vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
    vol_ratio = _num(df["Volume"].iloc[-1] / vol_avg, 2) if vol_avg else None
    hi, lo = float(close.tail(252).max()), float(close.tail(252).min())

    have200 = not math.isnan(sma200)
    if have200 and c > sma50 > sma200:
        trend = "Uptrend (above 50 & 200-day averages)"
    elif have200 and c < sma50 < sma200:
        trend = "Downtrend (below 50 & 200-day averages)"
    elif not have200 and c > sma50:
        trend = "Uptrend (above 50-day average)"
    elif not have200 and c < sma50:
        trend = "Downtrend (below 50-day average)"
    else:
        trend = "Mixed"

    up = trend.startswith("Uptrend")
    overbought = rsi is not None and rsi >= 70
    warn = "RSI is high (overbought) - a pullback is possible." if overbought else None
    short = sym.replace(".NS", "").replace(".BO", "")

    tfs = [
        _setup("Intraday (watch)", up and c > sma20 and (vol_ratio or 0) >= 1 and not overbought,
               c, atr, 0.75, 1.5,
               "Trading above its 20-day average with normal-or-better volume.",
               "Needs an uptrend, price above the 20-day average and volume at or above average.", warn),
        _setup("Swing", up and rsi is not None and 40 <= rsi < 70,
               c, atr, 1.5, 2.0,
               "Uptrend with RSI in a healthy 40-70 zone.",
               "Needs an uptrend with RSI between 40 and 70.", warn),
        _setup("Positional", up and have200 and c > sma200 and not overbought,
               c, atr, 2.5, 2.0,
               "Price is above its 200-day average in a longer-term uptrend.",
               "Needs a long-term uptrend (price above the 200-day average, not overbought).", warn),
    ]
    for tf in tfs:
        tf["symbol"] = sym

    return {
        "name": name or short,
        "symbol": sym,
        "symbol_short": short,
        "exchange": "BSE" if sym.endswith(".BO") else "NSE",
        "live_price": None,
        "live_price_source": None,
        "snapshot": {
            "close": _num(c),
            "day_change_pct": _num((c - prev) / prev * 100, 2),
            "trend": trend,
            "rsi": rsi,
            "week_range_low": _num(lo),
            "week_range_high": _num(hi),
            "volume_vs_20d_avg": vol_ratio,
        },
        "timeframes": tfs,
        "news": [],
        "warnings": [],
        "data_note": "Computed from daily candles at the last scheduled update. "
                     "Educational only - not investment advice.",
    }


def build_search(symbols, names=None):
    names = names or {}
    out = {}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            raw = yf.download(chunk, period="1y", interval="1d", progress=False,
                              auto_adjust=True, group_by="ticker", threads=True)
        except Exception as e:
            print(f"  search batch failed: {e}")
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in chunk:
            try:
                df = raw[sym] if multi else raw
                res = analyse(sym, df, names.get(sym))
                if res:
                    out[res["symbol_short"]] = res
            except Exception:
                continue
    print(f"  search data built for {len(out)} of {len(symbols)} symbols")
    return out or None
