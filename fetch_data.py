"""
fetch_data.py - run by GitHub Actions (see update-data.yml), not by a server.

  python fetch_data.py full    # whole-market scan + fundamentals + everything below
  python fetch_data.py light   # news, results, IPO, dividends, prices only

It reuses the existing logic in app.py / daily_stock_brief.py and writes plain
JSON files into ./data/, which the website reads. If one task fails, the
previous file is kept, so one bad run never blanks the site.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import app as A                       # the Flask server only starts under __main__, so this is safe
import daily_stock_brief as D
from daily_stock_brief import WATCHLIST, fetch_history, get_fundamental_picks, get_result_suggestion

DATA = "data"
os.makedirs(os.path.join(DATA, "cache"), exist_ok=True)
# These caches used to vanish after every run. Keeping them on the data branch means
# fundamentals are only re-fetched when stale and the stock list is only rebuilt weekly.
D.FUNDAMENTALS_CACHE_FILE = os.path.join(DATA, "cache", "fundamentals_cache.json")
D.UNIVERSE_CACHE_FILE = os.path.join(DATA, "cache", "scan_universe.json")
IST = ZoneInfo("Asia/Kolkata")
MAX_SUGGESTIONS = 60


def now_iso():
    return datetime.now(IST).isoformat()


def path(name):
    return os.path.join(DATA, name + ".json")


def read(name):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except Exception:
        return None


def write(name, obj):
    tmp = path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, path(name))
    print(f"  wrote data/{name}.json")


def task(name, fn):
    """Run one job; on any error keep the previous file and carry on."""
    t0 = time.time()
    print(f"[{name}] starting")
    try:
        out = fn()
        if out is not None:
            write(name, out)
        print(f"[{name}] done in {time.time() - t0:.0f}s")
        return out
    except Exception as e:
        print(f"[{name}] FAILED, keeping previous file: {e}")
        return None


# ---------------------------------------------------------------- price helpers
def get_prices(symbols, workers=8):
    """Latest price for many symbols in parallel (was one-by-one, the slowest part of a run)."""
    def one(sym):
        try:
            fast = yf.Ticker(sym).fast_info
            price = fast.get("lastPrice") or fast.get("last_price")
            return sym, round(float(price), 2) if price else None
        except Exception:
            return sym, None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {s: p for s, p in pool.map(one, list(symbols)) if p}


def fast_prices_detailed(symbols):
    return {s: {"price": p, "source": "yahoo"} for s, p in get_prices(symbols).items()}


A.fetch_live_prices_detailed = fast_prices_detailed     # report generation uses this too


# ---------------------------------------------------------------- heavy jobs
def job_report():
    return A.run_full_analysis()


def job_fundamentals(report):
    candidates = (report or read("report") or {}).get("liquid_stocks") or list(WATCHLIST)
    picks = get_fundamental_picks(candidates)
    now = datetime.now(IST)
    return {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%d %b %Y, %I:%M %p") + " IST",
        "candidates_checked": len(candidates),
        "picks": picks,
    }


# ---------------------------------------------------------------- light jobs
def job_news():
    return A.get_news_digest_cached(force_refresh=True)


def job_results():
    data = A.get_results_calendar_cached(force_refresh=True)
    if data.get("error") and not data.get("upcoming") and not data.get("published"):
        print("  NSE returned nothing for results - keeping previous file")
        return None
    return data


def job_ipo():
    data = A.get_ipo_dashboard(force_refresh=True)
    if not any(data.get(k) for k in ("current", "upcoming", "recent")) and read("ipo"):
        print("  IPO feed returned nothing - keeping previous file")
        return None
    return data


def job_dividends():
    data = A.get_dividend_picks_cached(force_refresh=True)
    if not data and read("dividends"):
        print("  dividend feed returned nothing - keeping previous file")
        return None
    return data


SUGGESTION_TTL_SECONDS = 6 * 3600


def job_suggestions():
    cal = read("results") or {}
    rows = ([(r, "upcoming") for r in cal.get("upcoming", [])] +
            [(r, "published") for r in cal.get("published", [])])[:MAX_SUGGESTIONS]
    old = read("suggestions") or {}
    stamps = read("cache/suggestions_meta") or {}
    now = time.time()
    out, todo = {}, []
    for r, status in rows:
        key = f"{r['symbol']}|{r['date']}|{status}"
        if key in old and now - stamps.get(key, 0) < SUGGESTION_TTL_SECONDS:
            out[key] = old[key]                      # still fresh: skip the slow re-analysis
        else:
            todo.append((key, r, status))
    print(f"  {len(out)} suggestions reused, {len(todo)} to compute")

    def work(item):
        key, r, status = item
        try:
            return key, get_result_suggestion(r["symbol"], r["date"], status)
        except Exception as e:
            print(f"  suggestion failed for {r['symbol']}: {e}")
            return key, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for key, sg in pool.map(work, todo):
            if sg:
                out[key] = sg
                stamps[key] = now
            elif key in old:
                out[key] = old[key]
    write("cache/suggestions_meta", {k: v for k, v in stamps.items() if k in out})
    return out


def tracked_symbols():
    syms = set(WATCHLIST)
    for s in (read("report") or {}).get("signals", []):
        syms.add(s["symbol"])
    for p in (read("fundamentals") or {}).get("picks", []):
        if p.get("symbol"):
            syms.add(p["symbol"])
    return sorted(syms)


def job_prices():
    got = get_prices(tracked_symbols())
    if not got:
        print("  no prices came back - keeping previous file")
        return None
    return {"updated_at": now_iso(), "prices": got, "sources": {s: "yahoo" for s in got}}


def job_history():
    """Last 30 closes per symbol, downloaded in a few batches instead of one call each."""
    syms = tracked_symbols()
    out = {}
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        try:
            raw = yf.download(chunk, period="2mo", interval="1d", progress=False,
                              auto_adjust=True, group_by="ticker", threads=True)
        except Exception as e:
            print(f"  history batch failed: {e}")
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in chunk:
            try:
                close = (raw[sym] if multi else raw)["Close"].dropna().tail(30)
                if len(close):
                    out[sym] = [{"date": ix.strftime("%d %b"), "close": round(float(v), 2)}
                                for ix, v in close.items()]
            except Exception:
                continue
    return out or None


def job_gold():
    """Global gold price (COMEX gold futures, USD per troy ounce) + USD/INR -> India-equivalent rates."""
    OZ_GRAMS = 31.1035

    def hourly(sym):
        return yf.Ticker(sym).history(period="5d", interval="1h")["Close"].dropna()

    series = hourly("GC=F")
    symbol = "GC=F"
    if series.empty:
        series, symbol = hourly("XAUUSD=X"), "XAUUSD=X"
    if series.empty:
        raise RuntimeError("no gold price data returned")
    usd = float(series.iloc[-1])

    prev = None
    try:
        fast = yf.Ticker(symbol).fast_info
        prev = fast.get("previous_close") or fast.get("previousClose")
    except Exception:
        pass
    if not prev:
        daily = yf.Ticker(symbol).history(period="5d", interval="1d")["Close"].dropna()
        prev = float(daily.iloc[-2]) if len(daily) > 1 else usd
    prev = float(prev)

    fx = hourly("USDINR=X")
    usdinr = float(fx.iloc[-1]) if not fx.empty else None

    out = {
        "updated_at": now_iso(),
        "source": "Yahoo Finance - COMEX gold futures (GC=F)" if symbol == "GC=F" else "Yahoo Finance - XAU/USD",
        "usd_per_oz": round(usd, 2),
        "usd_prev_close": round(prev, 2),
        "usd_change": round(usd - prev, 2),
        "usd_change_pct": round((usd - prev) / prev * 100, 2) if prev else None,
        "usdinr": round(usdinr, 2) if usdinr else None,
        "series": [{"t": ts.isoformat(), "usd": round(float(v), 2)} for ts, v in series.tail(120).items()],
    }
    if usdinr:
        gram24 = usd * usdinr / OZ_GRAMS
        out.update({
            "inr_per_gram_24k": round(gram24, 2),
            "inr_per_10g_24k": round(gram24 * 10, 0),
            "inr_per_10g_22k": round(gram24 * 10 * 22 / 24, 0),
            "inr_per_10g_18k": round(gram24 * 10 * 18 / 24, 0),
        })
    return out


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "light").lower()
    if mode == "gold":                       # hourly, takes about a minute
        task("gold", job_gold)
        meta = read("meta") or {}
        meta["last_gold_run"] = now_iso()
        write("meta", meta)
        return
    if mode != "full" and read("report") is None:
        print("No report yet - upgrading this run to a full scan")
        mode = "full"
    print(f"Mode: {mode}")

    report = None
    if mode == "full":
        report = task("report", job_report)
        task("fundamentals", lambda: job_fundamentals(report))
        task("history", job_history)
        task("dividends", job_dividends)
    elif read("dividends") is None:
        task("dividends", job_dividends)

    task("gold", job_gold)
    task("news", job_news)
    task("results", job_results)
    task("ipo", job_ipo)
    task("suggestions", job_suggestions)
    task("prices", job_prices)

    meta = read("meta") or {}
    meta["last_run"] = now_iso()
    meta["last_mode"] = mode
    if mode == "full":
        meta["last_full_run"] = now_iso()
    write("meta", meta)


if __name__ == "__main__":
    main()
