"""
app.py
The web app version of the daily stock brief - no phone install needed,
just open a URL in any browser (including your phone's).

Reuses all the analysis logic from daily_stock_brief.py - this file is
purely the web layer (server + page) on top of it.

HOW TO RUN LOCALLY:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000 in your browser.

(Deploying this so it's reachable from your phone anywhere, not just on
your own computer's network, is the hosting step we'll do next.)
"""

import json
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string, request, redirect
import yfinance as yf

try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None

try:
    from growwapi import GrowwAPI
except ImportError:
    GrowwAPI = None
try:
    import pyotp
except ImportError:
    pyotp = None

from daily_stock_brief import (
    WATCHLIST, fetch_all, analyze_stock, fetch_news,
    select_top_signals, MIN_RESULTS_PER_CATEGORY, fetch_live_prices,
    analyze_single_stock, fetch_history, build_ipo_dashboard, get_dividend_picks,
    get_latest_news_digest, get_scan_universe, fetch_all_batched, get_results_calendar, get_result_suggestion,
    get_fundamental_picks, FUNDAMENTAL_CANDIDATE_LIMIT,
)

app = Flask(__name__)

CACHE_FILE = "report_cache.json"
KITE_SESSION_FILE = "kite_session.json"
IPO_GMP_FILE = "ipo_gmp.json"

IPO_DASHBOARD_CACHE_TTL_SECONDS = 10 * 60  # NSE data doesn't change second-to-second
_ipo_dashboard_cache = {"data": None, "fetched_at": 0}


def load_gmp_overrides() -> dict:
    """{symbol: gmp_float} - the only thing the user has to type in by hand,
    since GMP has no free/official data source anywhere."""
    if not os.path.exists(IPO_GMP_FILE):
        return {}
    try:
        with open(IPO_GMP_FILE) as f:
            raw = json.load(f)
        return {sym: v["gmp"] for sym, v in raw.items()}
    except Exception:
        return {}


def save_gmp_override(symbol: str, gmp: float) -> None:
    data = {}
    if os.path.exists(IPO_GMP_FILE):
        try:
            with open(IPO_GMP_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[symbol] = {"gmp": gmp, "updated_at": _today_ist()}
    with open(IPO_GMP_FILE, "w") as f:
        json.dump(data, f)


def get_ipo_dashboard(force_refresh: bool = False) -> dict:
    """Cached wrapper around build_ipo_dashboard - NSE's IPO endpoints are
    slow (cookie handshake + throttling) so we don't hit them on every
    page load, only every IPO_DASHBOARD_CACHE_TTL_SECONDS or on request."""
    now = time.time()
    if (
        not force_refresh
        and _ipo_dashboard_cache["data"] is not None
        and now - _ipo_dashboard_cache["fetched_at"] < IPO_DASHBOARD_CACHE_TTL_SECONDS
    ):
        return _ipo_dashboard_cache["data"]
    data = build_ipo_dashboard(load_gmp_overrides())
    _ipo_dashboard_cache["data"] = data
    _ipo_dashboard_cache["fetched_at"] = now
    return data


@app.route("/api/ipo-dashboard", methods=["GET"])
def api_ipo_dashboard():
    force = request.args.get("refresh") == "1"
    return jsonify({"status": "ok", "data": get_ipo_dashboard(force_refresh=force)})


@app.route("/api/ipo-gmp", methods=["POST"])
def api_ipo_gmp():
    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip()
    gmp = body.get("gmp")
    if not symbol or gmp is None:
        return jsonify({"status": "error", "message": "symbol and gmp are required"}), 400
    try:
        gmp = float(gmp)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "gmp must be a number"}), 400
    save_gmp_override(symbol, gmp)
    # GMP just changed - force the next dashboard read to reflect it immediately.
    data = get_ipo_dashboard(force_refresh=True)
    return jsonify({"status": "ok", "data": data})


DIVIDEND_PICKS_CACHE_TTL_SECONDS = 30 * 60  # per-stock history fetches aren't cheap; refresh every ~30 min
_dividend_picks_cache = {"data": None, "fetched_at": 0}


def get_dividend_picks_cached(force_refresh: bool = False) -> list:
    now = time.time()
    if (
        not force_refresh
        and _dividend_picks_cache["data"] is not None
        and now - _dividend_picks_cache["fetched_at"] < DIVIDEND_PICKS_CACHE_TTL_SECONDS
    ):
        return _dividend_picks_cache["data"]
    data = get_dividend_picks()
    _dividend_picks_cache["data"] = data
    _dividend_picks_cache["fetched_at"] = now
    return data


@app.route("/api/dividend-picks", methods=["GET"])
def api_dividend_picks():
    """
    Watchlist stocks going ex-dividend at least 14 days out, paired with a
    fresh technical setup - fully automatic (live NSE ex-date/amount data,
    no manual entry needed, and no "Generate report" click required).
    """
    force = request.args.get("refresh") == "1"
    return jsonify({"status": "ok", "data": get_dividend_picks_cached(force_refresh=force)})


AUTO_REFRESH_MINUTES = 15  # the page refreshes news / results / suggestions on this cycle
# Caches expire just under one cycle so every refresh really fetches fresh data.
NEWS_DIGEST_CACHE_TTL_SECONDS = (AUTO_REFRESH_MINUTES - 1) * 60
_news_digest_cache = {"data": None, "fetched_at": 0}


def get_news_digest_cached(force_refresh: bool = False) -> dict:
    now = time.time()
    if (
        not force_refresh
        and _news_digest_cache["data"] is not None
        and now - _news_digest_cache["fetched_at"] < NEWS_DIGEST_CACHE_TTL_SECONDS
    ):
        return _news_digest_cache["data"]
    data = get_latest_news_digest()
    _news_digest_cache["data"] = data
    _news_digest_cache["fetched_at"] = now
    return data


@app.route("/api/market-news", methods=["GET"])
def api_market_news():
    """
    Indian market + world headlines (free Google News RSS, no API key)
    paired with a simple keyword-based "what this could mean" note - not
    real analysis, just a plain-English starting point.
    """
    force = request.args.get("refresh") == "1"
    return jsonify({"status": "ok", "data": get_news_digest_cached(force_refresh=force)})


RESULTS_CALENDAR_CACHE_TTL_SECONDS = (AUTO_REFRESH_MINUTES - 1) * 60
_results_calendar_cache = {"data": None, "fetched_at": 0}


def get_results_calendar_cached(force_refresh: bool = False) -> dict:
    now = time.time()
    cached = _results_calendar_cache["data"]
    # Don't keep serving a failed (empty + error) result for a full hour.
    failed = cached is not None and cached.get("error") and not cached.get("upcoming") and not cached.get("published")
    ttl = 5 * 60 if failed else RESULTS_CALENDAR_CACHE_TTL_SECONDS
    if not force_refresh and cached is not None and now - _results_calendar_cache["fetched_at"] < ttl:
        return cached
    data = get_results_calendar()
    _results_calendar_cache["data"] = data
    _results_calendar_cache["fetched_at"] = now
    return data


@app.route("/api/results-calendar", methods=["GET"])
def api_results_calendar():
    """Quarterly results filed this month + board meetings scheduled in the next 30 days (NSE feed)."""
    force = request.args.get("refresh") == "1"
    return jsonify({"status": "ok", "data": get_results_calendar_cached(force_refresh=force)})


KITE_API_KEY = os.environ.get("KITE_API_KEY")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET")

GROWW_SESSION_FILE = "groww_session.json"
GROWW_API_KEY = os.environ.get("GROWW_API_KEY")          # the "TOTP token" from Groww's API keys page
GROWW_TOTP_SECRET = os.environ.get("GROWW_TOTP_SECRET")  # the "TOTP Secret" from the same page


def _today_ist() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


def _load_kite_access_token() -> str | None:
    """Kite Connect access tokens are only valid for the trading day they
    were issued - so a session saved on a previous date is treated as
    expired even if the file is still on disk."""
    if not os.path.exists(KITE_SESSION_FILE):
        return None
    try:
        with open(KITE_SESSION_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today_ist():
            return data.get("access_token")
    except Exception:
        pass
    return None


def _save_kite_access_token(access_token: str) -> None:
    with open(KITE_SESSION_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": _today_ist()}, f)


def get_kite_client():
    """Returns an authenticated KiteConnect client if today's Zerodha
    session is still valid, else None (caller should fall back to Yahoo)."""
    if not KiteConnect or not KITE_API_KEY:
        return None
    access_token = _load_kite_access_token()
    if not access_token:
        return None
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(access_token)
    return kite


def kite_status() -> dict:
    return {
        "configured": bool(KiteConnect and KITE_API_KEY and KITE_API_SECRET),
        "connected": get_kite_client() is not None,
    }


@app.route("/kite/login")
def kite_login():
    if not KiteConnect or not KITE_API_KEY:
        return (
            "Zerodha Kite Connect isn't configured on this server yet - "
            "set KITE_API_KEY (and KITE_API_SECRET) as environment variables first.",
            400,
        )
    kite = KiteConnect(api_key=KITE_API_KEY)
    return redirect(kite.login_url())


@app.route("/kite/callback")
def kite_callback():
    req_status = request.args.get("status")
    request_token = request.args.get("request_token")
    if req_status != "success" or not request_token:
        return "Zerodha login wasn't completed. <a href='/'>Go back to Daily Brief</a>", 400
    if not KiteConnect or not KITE_API_KEY or not KITE_API_SECRET:
        return "Zerodha Kite Connect isn't fully configured on this server (missing API key/secret).", 400
    kite = KiteConnect(api_key=KITE_API_KEY)
    try:
        session_data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        _save_kite_access_token(session_data["access_token"])
    except Exception as e:
        return f"Failed to complete Zerodha login: {e}. <a href='/'>Go back</a>", 400
    return redirect("/")


@app.route("/api/kite-status")
def api_kite_status():
    return jsonify(kite_status())


def _load_groww_access_token() -> str | None:
    """Groww's TOTP credentials themselves don't expire, but the access
    token they generate is only valid for the trading day it was issued."""
    if not os.path.exists(GROWW_SESSION_FILE):
        return None
    try:
        with open(GROWW_SESSION_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today_ist():
            return data.get("access_token")
    except Exception:
        pass
    return None


def _save_groww_access_token(access_token: str) -> None:
    with open(GROWW_SESSION_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": _today_ist()}, f)


def get_groww_client():
    """
    Returns an authenticated GrowwAPI client, auto-generating/refreshing
    today's access token via TOTP as needed. Unlike Kite Connect, this
    needs no daily manual browser login - just the two env vars below -
    because Groww's TOTP credentials don't expire, only the resulting
    daily access token does.
    """
    if not GrowwAPI or not pyotp or not GROWW_API_KEY or not GROWW_TOTP_SECRET:
        return None
    access_token = _load_groww_access_token()
    if not access_token:
        try:
            totp = pyotp.TOTP(GROWW_TOTP_SECRET).now()
            access_token = GrowwAPI.get_access_token(api_key=GROWW_API_KEY, totp=totp)
            _save_groww_access_token(access_token)
        except Exception as e:
            print(f"  [!] Groww auth failed: {e}")
            return None
    try:
        return GrowwAPI(access_token)
    except Exception as e:
        print(f"  [!] Groww client init failed: {e}")
        return None


def groww_status() -> dict:
    return {
        "configured": bool(GrowwAPI and pyotp and GROWW_API_KEY and GROWW_TOTP_SECRET),
        "connected": get_groww_client() is not None,
    }


@app.route("/api/data-status")
def api_data_status():
    """Combined status for both broker integrations, so the frontend can
    show whichever one is actually live without guessing."""
    return jsonify({"groww": groww_status(), "kite": kite_status()})


def fetch_live_prices_groww(symbols: list) -> dict:
    """Genuine real-time LTP via the Groww Trading API - tried first since
    that's the broker this deployment is actually configured for. Requires
    an active Groww Live Data subscription; returns {} if not connected."""
    groww = get_groww_client()
    if not groww or not symbols:
        return {}
    lookup = {}
    keys = []
    for symbol in symbols:
        base = symbol.replace(".NS", "").replace(".BO", "")
        exch = "BSE" if symbol.endswith(".BO") else "NSE"
        key = f"{exch}_{base}"
        keys.append(key)
        lookup[key] = symbol
    try:
        resp = groww.get_ltp(segment=groww.SEGMENT_CASH, exchange_trading_symbols=tuple(keys))
    except Exception as e:
        print(f"  [!] Groww LTP fetch failed: {e}")
        return {}
    out = {}
    for key, price in (resp or {}).items():
        symbol = lookup.get(key)
        if symbol and price:
            out[symbol] = round(float(price), 2)
    return out


def fetch_live_prices_kite(symbols: list) -> dict:
    """Genuine real-time last-traded-price via Zerodha Kite Connect - only
    used when a session is currently active (see get_kite_client)."""
    kite = get_kite_client()
    if not kite or not symbols:
        return {}
    lookup = {}
    kite_keys = []
    for symbol in symbols:
        base = symbol.replace(".NS", "").replace(".BO", "")
        exch = "BSE" if symbol.endswith(".BO") else "NSE"
        key = f"{exch}:{base}"
        kite_keys.append(key)
        lookup[key] = symbol
    try:
        data = kite.ltp(kite_keys)
    except Exception as e:
        print(f"  [!] Kite LTP fetch failed: {e}")
        return {}
    out = {}
    for key, val in data.items():
        symbol = lookup.get(key)
        price = (val or {}).get("last_price")
        if symbol and price:
            out[symbol] = round(float(price), 2)
    return out


def fetch_live_prices_detailed(symbols: list) -> dict:
    """
    Returns {symbol: {"price": float, "source": "groww" | "kite" | "yahoo"}}.
    Tries Groww first (the broker actually configured here), then Zerodha
    Kite Connect, then falls back to Yahoo Finance (free, delayed by
    seconds-to-~15min) for anything neither broker covered.
    """
    out = {}
    for symbol, price in fetch_live_prices_groww(symbols).items():
        out[symbol] = {"price": price, "source": "groww"}

    remaining = [s for s in symbols if s not in out]
    for symbol, price in fetch_live_prices_kite(remaining).items():
        out[symbol] = {"price": price, "source": "kite"}

    remaining = [s for s in symbols if s not in out]
    for symbol in remaining:
        try:
            ticker = yf.Ticker(symbol)
            fast = ticker.fast_info
            price = fast.get("lastPrice") or fast.get("last_price")
            if price:
                out[symbol] = {"price": round(float(price), 2), "source": "yahoo"}
        except Exception as e:
            print(f"  [!] Live price fetch failed for {symbol}: {e}")
    return out


def fetch_live_prices(symbols: list) -> dict:
    """Back-compat wrapper: {symbol: price}, source-agnostic. Prefer
    fetch_live_prices_detailed for anything shown to the user, so the UI
    can honestly label real-time (Zerodha) vs delayed (Yahoo) data."""
    return {sym: v["price"] for sym, v in fetch_live_prices_detailed(symbols).items()}


# Simple, honest keyword check - not real sentiment analysis, just a safety
# net so an obviously negative headline never sits silently next to a
# "Bullish" badge without at least a visible warning.
NEGATIVE_NEWS_KEYWORDS = [
    "fall", "falls", "falling", "crash", "plunge", "tumble", "slump",
    "sell-off", "selloff", "drop", "drops", "decline", "declines",
    "slip", "slips", "slide", "slides", "downgrade", "loss", "losses",
]


def check_news_conflict(headlines: list) -> str | None:
    """Returns a warning string if any headline looks negative, else None."""
    for h in headlines:
        title_lower = h.get("title", "").lower()
        if any(kw in title_lower for kw in NEGATIVE_NEWS_KEYWORDS):
            return f'Recent news may conflict with this signal: "{h["title"]}"'
    return None


# Shared progress for the background scan (read by /api/scan-status).
_scan_status = {"running": False, "done": 0, "total": 0, "phase": "", "started_at": None, "error": None}


def _scan_progress(done: int, total: int) -> None:
    _scan_status.update(done=done, total=total)


def run_full_analysis() -> dict:
    """Runs the whole pipeline and returns a JSON-serializable dict."""
    _scan_status["phase"] = "Loading the list of all NSE/BSE main-board stocks"
    universe = get_scan_universe()
    _scan_status.update(phase="Downloading price history", done=0, total=len(universe))
    price_data = fetch_all_batched(universe, progress_cb=_scan_progress)
    _scan_status["phase"] = "Analysing signals"

    all_candidates = []
    for symbol, df in price_data.items():
        all_candidates.extend(analyze_stock(symbol, df))

    # Fetch live prices for a generous pool per style (3x what we need), so
    # that if some top-ranked candidates turn out to already be invalidated
    # by today's actual trading, there's room to backfill from the rest of
    # the ranked list rather than just showing fewer results.
    by_style_preview: dict = {}
    for c in all_candidates:
        by_style_preview.setdefault(c["style"], []).append(c)
    pool_symbols = set()
    for style_candidates in by_style_preview.values():
        style_candidates.sort(key=lambda c: c["rank_score"])
        for c in style_candidates[:MIN_RESULTS_PER_CATEGORY * 3]:
            pool_symbols.add(c["symbol"])
    live_prices = fetch_live_prices(list(pool_symbols))

    # Keep the top MIN_RESULTS_PER_CATEGORY per style, ranked by strength,
    # validated against live price - instead of an all-or-nothing threshold,
    # and never showing a setup that's already broken by today's real move.
    all_signals = select_top_signals(all_candidates, n=MIN_RESULTS_PER_CATEGORY, live_prices=live_prices)

    news_by_symbol = {}
    flagged_symbols = {s["symbol"] for s in all_signals}
    from concurrent.futures import ThreadPoolExecutor as _TPE

    def _news(sym):
        try:
            return sym, fetch_news(sym)
        except Exception:
            return sym, []
    with _TPE(max_workers=6) as _pool:
        for sym, items in _pool.map(_news, list(flagged_symbols)):
            news_by_symbol[sym] = items

    # Cross-check every signal against the CURRENT live price, not just the
    # previous close it was calculated from. A moving-average-based signal
    # can lag real events by days - this catches the gap instead of hiding it.
    live_prices_detailed = fetch_live_prices_detailed(list(flagged_symbols))
    live_prices = {sym: v["price"] for sym, v in live_prices_detailed.items()}

    ipo_dashboard = get_ipo_dashboard()
    # The compact Signals-tab IPO summary only shows entries with a verdict
    # (i.e. GMP already entered) - the full live calendar (including ones
    # still needing a GMP) lives in the dedicated IPO Watch tab.
    ipo_list = [ipo for ipo in ipo_dashboard["current"] if ipo["gmp"] is not None]

    for s in all_signals:
        s["news"] = news_by_symbol.get(s["symbol"], [])
        s["symbol_short"] = s["symbol"].replace(".NS", "").replace(".BO", "")

        warnings = []
        live = live_prices.get(s["symbol"])
        s["live_price"] = live
        s["live_price_source"] = live_prices_detailed.get(s["symbol"], {}).get("source")
        if live is not None:
            pct_diff = round((live - s["entry"]) / s["entry"] * 100, 2)
            s["price_diff_pct"] = pct_diff
            if live <= s["stoploss"]:
                warnings.append(
                    f"Live price (₹{live}) is already at/below the stoploss - this setup may already be invalidated"
                )
            elif live >= s["target"]:
                warnings.append(
                    f"Live price (₹{live}) has already reached the target - this setup has already played out"
                )
            elif abs(pct_diff) >= 1.5:
                warnings.append(
                    f"Live price (₹{live}) has moved {pct_diff:+.1f}% from the calculated entry - re-check before acting"
                )
            elif s["style"] == "Intraday (watch)" and live > s["entry"]:
                # Intraday's entry is a breakout trigger (yesterday's resistance),
                # not an immediate buy-at-close price like Swing/Positional use -
                # so being above it, even slightly, means the breakout may have
                # already started before you're looking at this.
                warnings.append(
                    f"Live price (₹{live}) is already {pct_diff:+.1f}% above the breakout trigger - "
                    f"the move may already be underway, not waiting at the entry level"
                )
        else:
            s["price_diff_pct"] = None
            warnings.append("Could not fetch a live price to verify this signal - re-check manually before acting")

        news_conflict = check_news_conflict(s["news"])
        if news_conflict:
            warnings.append(news_conflict)

        s["warnings"] = warnings

    traded_value = {sym: float((df["Close"] * df["Volume"]).tail(20).mean()) for sym, df in price_data.items()}
    liquid_stocks = sorted(traded_value, key=traded_value.get, reverse=True)[:FUNDAMENTAL_CANDIDATE_LIMIT]

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))

    result = {
        "generated_at": now_ist.isoformat(),
        "generated_at_display": now_ist.strftime("%A, %d %B %Y - %I:%M %p") + " IST",
        "stocks_scanned": len(price_data),
        "universe_size": len(universe),
        "liquid_stocks": liquid_stocks,
        "signals": all_signals,
        "ipos": ipo_list,
    }

    with open(CACHE_FILE, "w") as f:
        json.dump(result, f)

    return result


def load_cached_report() -> dict | None:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return None


def _is_from_today_ist(iso_timestamp: str) -> bool:
    """The report's Entry/Stoploss/Target levels are calculated from the
    most recent COMPLETED trading day's close - regenerating more than
    once a day recomputes the exact same levels for no benefit, while
    hammering yfinance/NSE for nothing. So "fresh" here means "generated
    at all today", not "generated in the last N minutes"; live prices
    (which DO need to be frequent) are already handled separately by
    /api/live-price, polled every 60s independent of this."""
    if not iso_timestamp:
        return False
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return False
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return dt.astimezone(ZoneInfo("Asia/Kolkata")).date() == today_ist


# Prevents two overlapping full scans (e.g. two browser tabs opened at once,
# both finding a stale cache) from both hammering yfinance/NSE at the same
# time - the second caller just waits for the first's result instead.
_report_generation_lock = threading.Lock()


@app.route("/api/report", methods=["GET"])
def api_get_report():
    data = load_cached_report()
    if data is None:
        return jsonify({"status": "empty", "stale": True})
    stale = not _is_from_today_ist(data.get("generated_at", ""))
    return jsonify({"status": "ok", "data": data, "stale": stale})


def _scan_worker() -> None:
    try:
        run_full_analysis()
    except Exception as e:                       # surfaced to the UI via /api/scan-status
        print(f"  [!] Scan failed: {e}")
        _scan_status["error"] = str(e)
    finally:
        _scan_status["running"] = False
        _report_generation_lock.release()


def start_background_scan() -> bool:
    """Starts a full-market scan in a background thread. False if one is already running."""
    if not _report_generation_lock.acquire(blocking=False):
        return False
    _scan_status.update(running=True, done=0, total=0, phase="Starting", started_at=time.time(), error=None)
    threading.Thread(target=_scan_worker, daemon=True).start()
    return True


@app.route("/api/generate", methods=["POST"])
def api_generate_report():
    # force=1 lets the manual "Refresh now" button regenerate even when
    # today's cache is already fresh; the automatic once-a-day check never
    # sends this, so it never re-triggers a same-day scan on its own.
    force = request.args.get("force") == "1"

    if not force:
        cached = load_cached_report()
        if cached is not None and _is_from_today_ist(cached.get("generated_at", "")):
            return jsonify({"status": "ok", "data": cached, "already_fresh": True})

    # A scan of the whole market takes minutes, so it runs in the background
    # and the page polls /api/scan-status instead of holding one request open.
    started = start_background_scan()
    return jsonify({"status": "busy", "started": started, "message": "Market scan in progress."}), 202


@app.route("/api/scan-status", methods=["GET"])
def api_scan_status():
    return jsonify(_scan_status)


_suggestion_cache = {}          # (symbol, date, status) -> (timestamp, suggestion)
SUGGESTION_CACHE_TTL_SECONDS = (AUTO_REFRESH_MINUTES - 1) * 60
MAX_SUGGESTIONS_PER_REQUEST = 12


@app.route("/api/results-suggestions", methods=["POST"])
def api_results_suggestions():
    """Buy/wait/avoid read (+ news and Entry/SL/Target) for stocks listed in Quarterly Results."""
    from concurrent.futures import ThreadPoolExecutor
    body = request.get_json(silent=True) or {}
    items = body.get("items", [])[:MAX_SUGGESTIONS_PER_REQUEST]
    bypass_cache = bool(body.get("refresh"))
    now = time.time()
    results, todo = {}, []
    for it in items:
        sym, rdate, status = str(it.get("symbol", "")), it.get("date"), it.get("status", "upcoming")
        if not sym:
            continue
        key = f"{sym}|{rdate}|{status}"
        hit = _suggestion_cache.get(key)
        if hit and not bypass_cache and now - hit[0] < SUGGESTION_CACHE_TTL_SECONDS:
            results[key] = hit[1]
        else:
            todo.append((key, sym, rdate, status))

    def work(t):
        key, sym, rdate, status = t
        try:
            return key, get_result_suggestion(sym, rdate, status)
        except Exception as e:
            print(f"  [!] Suggestion failed for {sym}: {e}")
            return key, {"symbol": sym, "verdict": "No data", "verdict_class": "neutral",
                         "summary": "Could not analyse this stock right now.", "notes": [],
                         "setups": [], "news": [], "news_tone": "neutral", "snapshot": None}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, sg in pool.map(work, todo):
            _suggestion_cache[key] = (now, sg)
            results[key] = sg
    return jsonify({"status": "ok", "data": results})


SCAN_AFTER_HOUR_IST = 16   # NSE closes 15:30; give the daily bars time to settle
# The daily signals (Entry/SL/Target) are built from daily candles, so by default the
# whole-market scan runs once per trading day after the close. Set this True to re-run
# it every AUTO_REFRESH_MINUTES during market hours as well (9:15-15:45 IST). That is a
# multi-minute download of thousands of stocks each time, so Yahoo may throttle you.
RESCAN_FULL_MARKET_EVERY_CYCLE = False


def _scheduler_loop() -> None:
    """Unattended daily scan: after the close on weekdays, if today's post-close report doesn't exist yet."""
    while True:
        try:
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            in_market_hours = (9, 15) <= (now.hour, now.minute) <= (15, 45)
            if RESCAN_FULL_MARKET_EVERY_CYCLE and now.weekday() < 5 and in_market_hours \
                    and not _scan_status["running"]:
                cached = load_cached_report()
                age = (now - datetime.fromisoformat(cached["generated_at"])).total_seconds() if cached else 1e9
                if age >= AUTO_REFRESH_MINUTES * 60:
                    start_background_scan()
            if now.weekday() < 5 and now.hour >= SCAN_AFTER_HOUR_IST:
                cached = load_cached_report()
                done_today = False
                if cached:
                    gen = datetime.fromisoformat(cached["generated_at"]).astimezone(ZoneInfo("Asia/Kolkata"))
                    done_today = gen.date() == now.date() and gen.hour >= SCAN_AFTER_HOUR_IST
                if not done_today and not _scan_status["running"]:
                    start_background_scan()
        except Exception as e:
            print(f"  [!] Scheduler check failed: {e}")
        time.sleep(300)


FUND_PICKS_FILE = "fundamental_picks.json"
FUND_PICKS_TTL_SECONDS = 12 * 60 * 60     # levels come from daily candles; fundamentals change quarterly
_fund_status = {"running": False, "done": 0, "total": 0, "phase": "", "error": None}
_fund_lock = threading.Lock()


def _load_fund_picks():
    try:
        with open(FUND_PICKS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _fund_worker() -> None:
    try:
        cached = load_cached_report()
        candidates = (cached or {}).get("liquid_stocks") or list(WATCHLIST)
        _fund_status.update(phase="Checking fundamentals", done=0, total=len(candidates))
        picks = get_fundamental_picks(
            candidates, progress_cb=lambda d, t: _fund_status.update(done=d, total=t))
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        payload = {
            "generated_at": now_ist.isoformat(),
            "generated_at_display": now_ist.strftime("%d %b %Y, %I:%M %p") + " IST",
            "candidates_checked": len(candidates),
            "picks": picks,
        }
        with open(FUND_PICKS_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"  [!] Fundamentals screen failed: {e}")
        _fund_status["error"] = str(e)
    finally:
        _fund_status["running"] = False
        _fund_lock.release()


def start_fund_job() -> bool:
    if not _fund_lock.acquire(blocking=False):
        return False
    _fund_status.update(running=True, done=0, total=0, phase="Starting", error=None)
    threading.Thread(target=_fund_worker, daemon=True).start()
    return True


@app.route("/api/fundamental-picks", methods=["GET"])
def api_fundamental_picks():
    """Fundamentally strong stocks with price, Entry/SL/Target and timeline.
    Served from disk; rebuilt in the background when older than 12h (old data keeps showing meanwhile)."""
    force = request.args.get("refresh") == "1"
    payload = _load_fund_picks()
    stale = True
    if payload:
        try:
            age = (datetime.now(ZoneInfo("Asia/Kolkata")) - datetime.fromisoformat(payload["generated_at"])).total_seconds()
            stale = age >= FUND_PICKS_TTL_SECONDS
        except Exception:
            stale = True
    if (payload is None or stale or force) and not _fund_status["running"]:
        start_fund_job()
    body = {"status": "ok" if payload else "busy", "data": payload,
            "refreshing": _fund_status["running"], "progress": _fund_status}
    return jsonify(body), (200 if payload else 202)


@app.route("/api/live-price", methods=["GET"])
def api_live_price():
    """
    Returns current/latest price (and its source - "kite" for genuine
    real-time, "yahoo" for the free delayed fallback) for a comma-separated
    list of symbols.
    """
    symbols_param = request.args.get("symbols", "")
    symbols = [s.strip() for s in symbols_param.split(",") if s.strip()]
    if not symbols:
        return jsonify({"status": "error", "message": "no symbols provided"}), 400

    detailed = fetch_live_prices_detailed(symbols)
    prices = {sym: v["price"] for sym, v in detailed.items()}
    sources = {sym: v["source"] for sym, v in detailed.items()}
    return jsonify({"status": "ok", "prices": prices, "sources": sources, "kite": kite_status()})


# Honest note on "real-time": there is no free, official, tick-by-tick NSE/BSE
# feed. This uses Yahoo Finance's public quote data, which is generally the
# most current freely-available price but can lag the live exchange price by
# a few seconds up to ~15 minutes depending on market conditions and Yahoo's
# own caching. It is NOT an exchange-certified real-time feed. A live,
# guaranteed-accurate tick feed would require a paid broker/market-data API
# (e.g. a Zerodha Kite Connect, Upstox, or NSE-licensed data subscription).
DATA_ACCURACY_NOTE = (
    "When Zerodha is connected, live prices are genuine real-time last-traded-price "
    "quotes from Kite Connect. Otherwise this falls back to Yahoo Finance's free "
    "NSE/BSE quote data - typically current to within a few seconds to ~15 minutes, "
    "not an exchange-certified real-time feed. Each price below is labelled with its "
    "actual source so it's never ambiguous which one you're looking at."
)


@app.route("/api/search", methods=["GET"])
def api_search_stock():
    """
    Free-text search for ANY NSE/BSE-listed stock (not limited to the Nifty
    100 watchlist): resolves the symbol, runs the same Entry/Stoploss/Target
    rule engines used in the daily scan, and cross-checks against the latest
    fetchable price - same logic as the main report, just on-demand for one
    stock instead of the fixed watchlist.
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Type a stock name or symbol to search."}), 400

    result = analyze_single_stock(query)

    if result["status"] == "multiple_matches":
        return jsonify({"status": "ok", "data": result})

    if result["status"] == "not_found":
        return jsonify({
            "status": "error",
            "message": (
                f'Couldn\'t find a tradable NSE/BSE symbol matching "{query}". '
                f"Try the exact ticker (e.g. RELIANCE, ZOMATO, IRCTC)."
            ),
        })

    symbol = result["symbol"]
    live_detailed = fetch_live_prices_detailed([symbol])
    live_info = live_detailed.get(symbol)
    live = live_info["price"] if live_info else None
    result["live_price"] = live
    result["live_price_source"] = live_info["source"] if live_info else None

    try:
        result["news"] = fetch_news(symbol)
    except Exception:
        result["news"] = []

    warnings = []
    if result.get("limited_history_note"):
        warnings.append(result["limited_history_note"])
    if live is not None:
        close = result["snapshot"]["close"]
        result["snapshot"]["live_vs_close_diff_pct"] = round((live - close) / close * 100, 2) if close else None
        for tf in result["timeframes"]:
            if not tf.get("has_setup"):
                continue
            if live <= tf["stoploss"]:
                tf["warning"] = f"Live price (\u20b9{live}) is already at/below the stoploss - this setup may already be invalidated"
            elif live >= tf["target"]:
                tf["warning"] = f"Live price (\u20b9{live}) has already reached the target - this setup has already played out"
            elif tf["style"] == "Intraday (watch)" and live > tf["entry"]:
                pct_diff = round((live - tf["entry"]) / tf["entry"] * 100, 2)
                tf["warning"] = (
                    f"Live price (\u20b9{live}) is already {pct_diff:+.1f}% above the breakout trigger - "
                    f"the move may already be underway, not waiting at the entry level"
                )
    else:
        warnings.append("Could not fetch a live price for this symbol right now - showing the last available close instead.")

    news_conflict = check_news_conflict(result["news"])
    if news_conflict:
        warnings.append(news_conflict)

    result["warnings"] = warnings
    result["data_note"] = DATA_ACCURACY_NOTE

    return jsonify({"status": "ok", "data": result})


@app.route("/api/history", methods=["GET"])
def api_history():
    """
    Returns the last ~30 trading days of closing prices for each requested
    symbol - used to draw the watchlist trend chart. The frontend appends
    the current live price as the final "Now" point on top of this.
    """
    symbols_param = request.args.get("symbols", "")
    symbols = [s.strip() for s in symbols_param.split(",") if s.strip()]
    if not symbols:
        return jsonify({"status": "error", "message": "no symbols provided"}), 400

    out = {}
    for symbol in symbols:
        try:
            df = fetch_history(symbol, days=45)
            if df is None or len(df) == 0:
                continue
            tail = df.tail(30)
            out[symbol] = [
                {"date": idx.strftime("%d %b"), "close": round(float(row["Close"]), 2)}
                for idx, row in tail.iterrows()
            ]
        except Exception as e:
            print(f"  [!] History fetch failed for {symbol}: {e}")
    return jsonify({"status": "ok", "history": out})


@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE)


PAGE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Brief</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #10141c;
    --surface: #171d28;
    --surface-raised: #1d2432;
    --text: #e7e9ee;
    --text-muted: #8a93a3;
    --gold: #d4a94f;
    --green: #4caf7d;
    --red: #d96b62;
    --border: #262e3d;
  }
  :root[data-theme="light"] {
    --bg: #f3f4f7;
    --surface: #ffffff;
    --surface-raised: #eceef3;
    --text: #171d28;
    --text-muted: #5b6474;
    --gold: #a5750f;
    --green: #22794f;
    --red: #a23e33;
    --border: #dadfe8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    -webkit-font-smoothing: antialiased;
    transition: background 0.2s ease, color 0.2s ease;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 28px 20px 60px; }

  /* --- Dashboard shell: sidebar + main --- */
  .app-shell { display: flex; min-height: 100vh; }
  .sidebar {
    width: 220px;
    flex-shrink: 0;
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 22px 14px;
    display: flex;
    flex-direction: column;
    position: sticky;
    top: 0;
    height: 100vh;
  }
  .sidebar-logo {
    font-family: 'Source Serif 4', serif;
    font-weight: 600;
    font-size: 1.25rem;
    padding: 0 10px 22px;
  }
  .sidebar-nav { display: flex; flex-direction: column; gap: 4px; }
  .nav-item {
    background: transparent;
    color: var(--text-muted);
    border: none;
    text-align: left;
    padding: 10px 12px;
    border-radius: 8px;
    font-family: inherit;
    font-weight: 500;
    font-size: 0.92rem;
    cursor: pointer;
  }
  .nav-item:hover { background: var(--surface-raised); color: var(--text); }
  .nav-item.active { background: rgba(212, 169, 79, 0.12); color: var(--gold); font-weight: 600; }
  .sidebar-footer {
    margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 10px;
  }
  .kite-status { font-size: 0.75rem; color: var(--text-muted); line-height: 1.4; }
  .kite-status a, .kite-status .link-btn {
    color: var(--gold); text-decoration: none; font-weight: 600;
    background: none; border: none; padding: 0; cursor: pointer;
    font-size: inherit; font-family: inherit;
  }
  .kite-status .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; }
  .kite-status .dot.on { background: var(--green); }
  .kite-status .dot.off { background: var(--text-muted); }

  .src-badge {
    font-size: 0.66rem; font-weight: 600; padding: 1px 6px; border-radius: 4px;
    margin-left: 6px; white-space: nowrap;
  }
  .src-badge.src-kite { color: var(--green); background: rgba(76,175,125,0.12); }
  .src-badge.src-yahoo { color: var(--gold); background: rgba(212,169,79,0.12); }

  .main-content { flex: 1; min-width: 0; padding: 24px 28px 60px; }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 14px;
  }
  .topbar h1 {
    font-family: 'Source Serif 4', serif;
    font-weight: 600;
    font-size: 1.7rem;
    margin: 0 0 2px;
    letter-spacing: -0.01em;
  }
  .topbar .date { color: var(--text-muted); font-size: 0.9rem; }
  .topbar-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

  .stat-cards {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 12px;
    margin-top: 22px;
  }
  @media (min-width: 780px) { .stat-cards { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); } }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .stat-card .stat-label { color: var(--text-muted); font-size: 0.8rem; margin-bottom: 6px; }
  .stat-card .stat-value {
    font-size: 1.5rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .stat-card .stat-value.gold { color: var(--gold); }

  .dashboard-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    margin-top: 16px;
  }
  @media (min-width: 900px) { .dashboard-grid { grid-template-columns: 2fr 1fr; } }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
  }
  .card .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .card .card-header h2 { font-size: 1rem; font-weight: 600; margin: 0; }
  .live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    margin-right: 5px;
    box-shadow: 0 0 0 rgba(76,175,125,0.5);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(76,175,125,0.45); }
    70% { box-shadow: 0 0 0 6px rgba(76,175,125,0); }
    100% { box-shadow: 0 0 0 0 rgba(76,175,125,0); }
  }
  .live-tag { font-size: 0.75rem; color: var(--text-muted); }

  .watchlist-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 9px 0;
    border-bottom: 1px solid var(--border);
  }
  .watchlist-row:last-child { border-bottom: none; }
  .watchlist-row .wl-name { font-weight: 600; font-size: 0.9rem; }
  .watchlist-row .wl-style { color: var(--text-muted); font-size: 0.72rem; }
  .watchlist-row .wl-price { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.9rem; }
  .watchlist-row .wl-change { text-align: right; font-size: 0.75rem; font-variant-numeric: tabular-nums; }

  .preview-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px;
    margin-top: 16px;
  }
  @media (min-width: 900px) { .preview-grid { grid-template-columns: repeat(3, 1fr); } }
  .preview-card .card-header h2 { font-size: 0.92rem; }
  .preview-card .card-header .style-duration { font-size: 0.75rem; }
  .view-all-link {
    display: inline-block;
    margin-top: 10px;
    background: transparent;
    border: none;
    color: var(--gold);
    font-family: inherit;
    font-weight: 600;
    font-size: 0.82rem;
    cursor: pointer;
    padding: 0;
  }
  .preview-empty { color: var(--text-muted); font-size: 0.85rem; padding: 6px 0; }

  .news-hint {
    font-size: 0.8rem; color: var(--text-muted); margin-bottom: 16px;
    padding-bottom: 12px; border-bottom: 1px solid var(--border);
  }
  .news-section-label {
    font-weight: 600; font-size: 0.78rem; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 8px;
  }
  .news-item { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .news-item:last-child { border-bottom: none; }
  .news-item .news-title { font-weight: 600; font-size: 0.92rem; line-height: 1.35; }
  .news-item .news-title a { color: var(--text); text-decoration: none; }
  .news-item .news-title a:hover { color: var(--gold); }
  .news-item .news-impact { font-size: 0.82rem; color: var(--text-muted); margin-top: 5px; line-height: 1.4; }
  .news-item .news-impact .impact-icon { margin-right: 3px; }

  .news-results-grid { display: grid; grid-template-columns: 1fr; gap: 16px; align-items: start; }
  @media (min-width: 900px) { .news-results-grid { grid-template-columns: 1fr 1fr; } }
  .results-tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .results-tab {
    background: transparent; border: 1px solid var(--border); color: var(--text-muted);
    border-radius: 999px; padding: 5px 12px; font-family: inherit; font-size: 0.8rem; cursor: pointer;
  }
  .results-tab.active { border-color: var(--gold); color: var(--gold); font-weight: 600; }
  .results-filter {
    width: 100%; box-sizing: border-box; margin-bottom: 8px; padding: 7px 10px; font-family: inherit;
    font-size: 0.85rem; background: transparent; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
  }
  .results-list { max-height: 520px; overflow-y: auto; }
  .refresh-eta { font-size: 0.78rem; color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .fund-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
  .fund-card { margin-bottom: 0; }
  .fund-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .fund-name { font-weight: 700; font-size: 0.98rem; line-height: 1.25; }
  .fund-sub { font-size: 0.76rem; color: var(--text-muted); margin-top: 2px; }
  .fund-score { min-width: 46px; text-align: center; font-weight: 700; font-size: 0.95rem; color: var(--green); border: 1px solid var(--green); border-radius: 8px; padding: 3px 6px; }
  .fund-score small { display: block; font-weight: 400; font-size: 0.6rem; color: var(--text-muted); }
  .fund-price-row { display: flex; align-items: baseline; gap: 10px; margin: 10px 0 8px; flex-wrap: wrap; }
  .fund-price { font-size: 1.25rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .fund-chg { font-size: 0.82rem; font-variant-numeric: tabular-nums; }
  .fund-status { font-size: 0.72rem; font-weight: 700; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); }
  .fund-status.buy { color: var(--green); border-color: var(--green); }
  .fund-status.wait { color: var(--gold); border-color: var(--gold); }
  .fund-levels { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 8px; }
  .fund-levels div { background: var(--surface-raised); border-radius: 6px; padding: 6px 8px; font-variant-numeric: tabular-nums; }
  .fund-levels small { display: block; font-size: 0.68rem; color: var(--text-muted); }
  .fund-levels b { font-size: 0.9rem; }
  .fund-levels .sl b { color: var(--red); }
  .fund-levels .tg b { color: var(--green); }
  .fund-timeline { font-size: 0.84rem; font-weight: 600; margin-bottom: 4px; }
  .fund-note { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; margin-bottom: 6px; }
  .fund-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
  .fund-tags span { font-size: 0.68rem; border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; color: var(--text-muted); }
  .fund-metrics { font-size: 0.74rem; color: var(--text-muted); }
  .res-item { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .res-item:last-child { border-bottom: none; }
  .res-row { display: flex; gap: 12px; align-items: baseline; }
  .res-row .res-main { flex: 1; min-width: 0; }
  .res-star { background: transparent; border: none; cursor: pointer; font-size: 1.15rem; line-height: 1; padding: 0 2px; color: var(--text-muted); }
  .res-star.on { color: var(--gold); }
  .n100-tag { font-size: 0.65rem; font-weight: 600; border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; margin-left: 6px; color: var(--text-muted); vertical-align: 1px; }
  .res-sugg { margin: 8px 0 0 76px; padding: 8px 10px; border-radius: 6px; background: var(--surface-raised); border-left: 3px solid var(--border); font-size: 0.8rem; line-height: 1.45; }
  .res-sugg.sugg-buy { border-left-color: var(--green); }
  .res-sugg.sugg-wait { border-left-color: var(--gold); }
  .res-sugg.sugg-avoid { border-left-color: var(--red); }
  .sugg-verdict { font-weight: 700; margin-right: 6px; }
  .sugg-buy .sugg-verdict { color: var(--green); }
  .sugg-wait .sugg-verdict { color: var(--gold); }
  .sugg-avoid .sugg-verdict { color: var(--red); }
  .sugg-line { color: var(--text-muted); margin-top: 3px; }
  .sugg-levels { margin-top: 5px; font-variant-numeric: tabular-nums; }
  .sugg-levels b { font-weight: 600; }
  .sugg-news a { color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border); }
  .sugg-news a:hover { border-bottom-color: var(--gold); }
  .res-more { background: transparent; border: 1px solid var(--border); color: var(--gold); border-radius: 6px; padding: 6px 12px; margin: 10px 0 2px; font-family: inherit; font-size: 0.82rem; cursor: pointer; }
  .res-date { min-width: 64px; font-weight: 600; font-size: 0.85rem; font-variant-numeric: tabular-nums; }
  .res-date small { display: block; font-weight: 400; color: var(--text-muted); font-size: 0.72rem; }
  .res-name { font-weight: 600; font-size: 0.88rem; line-height: 1.3; }
  .res-name a { color: var(--text); text-decoration: none; }
  .res-name a:hover { color: var(--gold); }
  .res-sub { font-size: 0.76rem; color: var(--text-muted); margin-top: 2px; }
  .ipo-section-title {
    font-size: 0.95rem; font-weight: 600; margin: 22px 0 10px;
    display: flex; align-items: center; gap: 8px;
  }
  .ipo-section-title:first-child { margin-top: 0; }
  .ipo-grid {
    display: grid; grid-template-columns: 1fr; gap: 12px;
  }
  @media (min-width: 780px) { .ipo-grid { grid-template-columns: repeat(2, 1fr); } }
  .ipo-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px;
  }
  .ipo-card-header {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 4px; margin-bottom: 8px;
  }
  .ipo-card .ipo-name { font-weight: 600; font-size: 0.95rem; }
  .ipo-card .ipo-dates { font-size: 0.78rem; color: var(--text-muted); }
  .ipo-figures {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px;
    margin-bottom: 10px; font-size: 0.85rem; font-variant-numeric: tabular-nums;
  }
  .ipo-gmp-row { border-top: 1px solid var(--border); padding-top: 10px; font-size: 0.85rem; }
  .ipo-gmp-row .gmp-form { display: flex; gap: 6px; align-items: center; }
  .ipo-gmp-row .gmp-form input {
    width: 100px; background: var(--surface-raised); border: 1px solid var(--border);
    color: var(--text); padding: 6px 8px; border-radius: 6px; font-family: inherit; font-size: 0.85rem;
  }
  .ipo-gmp-row .gmp-form button {
    padding: 6px 12px; font-size: 0.8rem;
  }
  .ipo-gmp-row .link-btn {
    background: none; border: none; color: var(--gold); cursor: pointer;
    font-family: inherit; font-size: 0.78rem; padding: 0; margin-left: 8px;
  }
  .recent-ipo-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 0.85rem;
  }
  .recent-ipo-row:last-child { border-bottom: none; }

  .div-card {
    background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--border);
    border-radius: 10px; padding: 14px 16px;
  }
  .div-card.has-setup { border-left-color: var(--green); }
  .div-card-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 8px; margin-bottom: 6px;
  }
  .div-card .div-name { font-weight: 600; font-size: 0.95rem; }
  .div-card .div-yield {
    text-align: right; font-size: 1.1rem; font-weight: 700; color: var(--gold);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .div-card .div-meta { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 10px; }

  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  @media (max-width: 780px) {
    .app-shell { flex-direction: column; }
    .sidebar {
      width: 100%; height: auto; position: static;
      flex-direction: row; align-items: center; padding: 12px 14px;
    }
    .sidebar-logo { padding: 0 12px 0 0; }
    .sidebar-nav { flex-direction: row; overflow-x: auto; }
    .sidebar-footer { margin-top: 0; padding-top: 0; border-top: none; margin-left: auto; }
    .main-content { padding: 18px 16px 50px; }
  }

  .masthead { display: none; }
  .masthead h1 {
    font-family: 'Source Serif 4', serif;
    font-weight: 600;
    font-size: 2.1rem;
    margin: 0 0 2px;
    letter-spacing: -0.01em;
  }
  .masthead .date { color: var(--text-muted); font-size: 0.95rem; }

  .theme-toggle {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    width: 40px;
    height: 40px;
    border-radius: 8px;
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .countdown {
    margin-top: 22px;
    padding: 18px 20px;
    background: var(--surface);
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  .countdown .label { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 4px; }
  .countdown .value {
    font-variant-numeric: tabular-nums;
    font-size: 2rem;
    font-weight: 600;
    color: var(--gold);
  }

  .search-box {
    margin-top: 22px;
    display: flex;
    gap: 8px;
  }
  .search-box input {
    flex: 1;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 11px 14px;
    border-radius: 8px;
    font-family: inherit;
    font-size: 0.95rem;
  }
  .search-box input:focus { outline: 2px solid var(--gold); outline-offset: -1px; }
  .search-hint { color: var(--text-muted); font-size: 0.78rem; margin-top: 6px; }

  #search-result { margin-top: 16px; }
  .search-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
  }
  .search-card .sc-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 4px;
  }
  .search-card .sc-header .sc-name { font-family: 'Source Serif 4', serif; font-size: 1.4rem; font-weight: 600; }
  .search-card .sc-header .sc-symbol { color: var(--text-muted); font-size: 0.9rem; margin-left: 6px; }
  .search-card .sc-price { font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .search-card .sc-change { font-size: 0.9rem; font-weight: 600; margin-left: 6px; }
  .search-card .sc-livetag {
    font-size: 0.72rem; color: var(--text-muted); font-weight: 400;
    display: block; margin-top: 2px;
  }
  .snapshot-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px 18px;
    margin: 14px 0 18px;
    padding: 14px;
    background: var(--surface-raised);
    border-radius: 8px;
    font-variant-numeric: tabular-nums;
  }
  @media (min-width: 640px) { .snapshot-grid { grid-template-columns: repeat(4, 1fr); } }
  .snapshot-grid div { font-size: 0.9rem; }
  .snapshot-grid .flabel { display: block; font-size: 0.72rem; color: var(--text-muted); margin-bottom: 1px; }
  .trend-badge {
    font-size: 0.72rem; font-weight: 600; padding: 2px 8px; border-radius: 4px;
  }
  .trend-badge.Uptrend { color: var(--green); background: rgba(76, 175, 125, 0.12); }
  .trend-badge.Downtrend { color: var(--red); background: rgba(217, 107, 98, 0.12); }
  .trend-badge.Mixed, .trend-badge.other { color: var(--gold); background: rgba(212, 169, 79, 0.12); }

  .tf-rows { display: grid; grid-template-columns: 1fr; gap: 10px; }
  @media (min-width: 780px) { .tf-rows { grid-template-columns: repeat(3, 1fr); } }
  .tf-row {
    background: var(--surface-raised);
    border-left: 3px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
  }
  .tf-row.has-setup { border-left-color: var(--green); }
  .tf-row .tf-title { font-weight: 600; font-size: 0.92rem; margin-bottom: 6px; }
  .tf-row .tf-duration { color: var(--text-muted); font-weight: 400; font-size: 0.78rem; }
  .tf-row .no-setup-tag {
    color: var(--text-muted); font-size: 0.78rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.02em;
  }

  .candidate-list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
  .candidate-list button {
    background: var(--surface-raised);
    color: var(--text);
    border: 1px solid var(--border);
    text-align: left;
    font-weight: 500;
  }

  .data-note {
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    line-height: 1.4;
  }

  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 26px;
    gap: 12px;
    flex-wrap: wrap;
  }
  .toolbar-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
  .meta { color: var(--text-muted); font-size: 0.85rem; }
  button {
    background: var(--gold);
    color: #171208;
    border: none;
    padding: 10px 16px;
    border-radius: 8px;
    font-family: inherit;
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: wait; }
  button.secondary {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
  }
  button.secondary.active {
    background: rgba(76, 175, 125, 0.15);
    border-color: var(--green);
    color: var(--green);
  }

  .star-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-muted);
    padding: 3px 9px;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 500;
    cursor: pointer;
  }
  .star-btn.selected {
    background: rgba(212, 169, 79, 0.15);
    border-color: var(--gold);
    color: var(--gold);
  }

  #my-selections { margin-top: 28px; }
  #my-selections h2 {
    font-size: 1rem; font-weight: 600; margin: 0 0 12px; color: var(--text);
  }

  .style-duration { color: var(--text-muted); font-weight: 400; font-size: 0.82rem; }
  details.caution-group { margin-top: 10px; }
  details.caution-group summary {
    cursor: pointer;
    color: var(--text-muted);
    font-size: 0.85rem;
    padding: 4px 0;
  }

  section.group { margin-top: 34px; }
  section.group h2 {
    font-size: 1rem;
    font-weight: 600;
    margin: 0 0 12px;
    color: var(--text);
  }
  section.group .count { color: var(--text-muted); font-weight: 400; }
  section.group .rows {
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
  }
  @media (min-width: 640px) {
    section.group .rows { grid-template-columns: repeat(2, 1fr); }
  }
  @media (min-width: 980px) {
    section.group .rows { grid-template-columns: repeat(3, 1fr); }
  }

  .row {
    background: var(--surface);
    border-left: 3px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }
  .row.verdict-strong, .row.verdict-good, .row.direction-bullish { border-left-color: var(--green); }
  .row.verdict-avoid, .row.verdict-caution, .row.direction-bearish { border-left-color: var(--red); }
  .row.verdict-moderate, .row.verdict-weak { border-left-color: var(--gold); }

  .row .name {
    font-weight: 600;
    font-size: 1.05rem;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .row .name .badge {
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 4px;
  }
  .badge.dir-bullish { color: var(--green); background: rgba(76, 175, 125, 0.12); }
  .badge.dir-bearish { color: var(--red); background: rgba(217, 107, 98, 0.12); }
  .badge.dir-unknown { color: var(--text-muted); background: rgba(138, 147, 163, 0.12); }
  .figures {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    font-variant-numeric: tabular-nums;
    margin-bottom: 8px;
  }
  .figures div { font-size: 0.95rem; }
  .figures .flabel { display: block; font-size: 0.72rem; color: var(--text-muted); margin-bottom: 1px; }
  .reason { color: var(--text-muted); font-size: 0.85rem; line-height: 1.4; }
  .live-price-row {
    font-size: 0.82rem;
    color: var(--text-muted);
    margin: -2px 0 8px;
    font-variant-numeric: tabular-nums;
  }
  .pct-tag { font-size: 0.78rem; font-weight: 500; font-variant-numeric: tabular-nums; }
  .avg-price-row {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    background: var(--surface-raised); border-radius: 6px;
    padding: 8px 10px; margin-bottom: 8px; font-size: 0.85rem;
  }
  .avg-price-row input {
    width: 90px; background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 5px 8px; border-radius: 6px;
    font-family: inherit; font-size: 0.82rem; font-variant-numeric: tabular-nums;
  }
  .avg-price-row .link-btn {
    background: none; border: none; color: var(--gold); cursor: pointer;
    font-family: inherit; font-size: 0.8rem; font-weight: 600; padding: 0;
  }
  .avg-price-row .pnl-tag { font-weight: 600; font-variant-numeric: tabular-nums; }
  .wl-remove-btn {
    background: none; border: none; color: var(--text-muted); cursor: pointer;
    font-size: 0.9rem; padding: 2px 4px; margin-left: 6px; line-height: 1;
  }
  .wl-remove-btn:hover { color: var(--red); }
  .caution-banner {
    background: rgba(217, 107, 98, 0.12);
    border: 1px solid var(--red);
    color: var(--red);
    font-size: 0.8rem;
    padding: 6px 10px;
    border-radius: 6px;
    margin-bottom: 8px;
  }
  .news { margin-top: 8px; font-size: 0.82rem; }
  .news a { color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border); }
  .news a:hover { border-bottom-color: var(--gold); }

  .empty {
    color: var(--text-muted);
    padding: 30px 0;
    text-align: center;
    font-size: 0.95rem;
  }
  .disclaimer {
    margin-top: 40px;
    padding-top: 18px;
    border-top: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 0.8rem;
    line-height: 1.5;
  }
  @media print {
    .theme-toggle, .toolbar-buttons, .topbar-actions, .star-btn, .countdown, .sidebar,
    .stat-cards, .dashboard-grid, #my-selections,
    details.caution-group summary { display: none !important; }
    .main-content { padding: 0 !important; }
    body, :root { background: #ffffff !important; color: #111 !important; }
    .row {
      background: #fff !important;
      border: 1px solid #ccc !important;
      break-inside: avoid;
    }
    .row.direction-bullish { border-left: 4px solid #2f8f5b !important; }
    .badge.dir-bullish { color: #2f8f5b !important; background: #eaf6ef !important; }
    .flabel, .reason, .meta, .style-duration, .count { color: #555 !important; }
    section.group .rows { grid-template-columns: repeat(2, 1fr) !important; }
    .wrap { max-width: 100% !important; }
    a { color: #111 !important; }
  }
</style>
</head>
<body>
<div class="app-shell">

  <aside class="sidebar">
    <div class="sidebar-logo">📈 Daily Brief</div>
    <nav class="sidebar-nav">
      <button class="nav-item active" data-tab="dashboard">🏠 Dashboard</button>
      <button class="nav-item" data-tab="signals">📊 Signals</button>
      <button class="nav-item" data-tab="ipo">🆕 IPO Watch</button>
      <button class="nav-item" data-tab="dividends">💰 Dividends</button>
      <button class="nav-item" data-tab="fundamentals">🏆 Fundamentals</button>
    </nav>
    <div class="sidebar-footer">
      <div id="kite-status" class="kite-status">Checking Zerodha…</div>
      <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" aria-label="Toggle light/dark theme">🌙</button>
    </div>
  </aside>

  <main class="main-content">

    <div class="topbar">
      <div>
        <h1>Dashboard</h1>
        <div class="date" id="today-date"></div>
      </div>
      <div class="topbar-actions">
        <button class="secondary" id="notify-btn" onclick="toggleNotifications()">🔔 Enable alerts</button>
        <button class="secondary" id="pdf-btn" onclick="downloadPdf()">⬇ PDF</button>
        <button id="generate-btn" onclick="generateReport()" title="Reports update automatically once per day - use this only to force an early refresh">Refresh now</button>
        <span class="refresh-eta" id="refresh-eta" title="The page refreshes news, results calendar, suggestions and live data every 15 minutes while it is open.">Next refresh in --:--</span>
      </div>
    </div>
    <div class="meta" id="last-generated" style="margin-top:8px">No report generated yet</div>
    <div class="meta" style="margin-top:2px">Live prices, news, results and suggestions refresh every 15 minutes · the full-market scan (Entry/SL/Target) runs once per trading day after the close</div>
    <div class="meta" id="auto-refresh-status" style="margin-top:2px;display:none;color:var(--gold)"></div>
    <div class="meta" id="refresh-log" style="margin-top:2px"></div>

    <!-- ===================== DASHBOARD TAB ===================== -->
    <div id="tab-dashboard" class="tab-panel active">

      <div class="stat-cards">
        <div class="stat-card">
          <div class="stat-label">Stocks scanned</div>
          <div class="stat-value" id="stat-scanned">--</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Signals today</div>
          <div class="stat-value" id="stat-signals">--</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Tracked stocks</div>
          <div class="stat-value" id="stat-tracked">--</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Market opens in</div>
          <div class="stat-value gold" id="countdown-value">--:--:--</div>
        </div>
        <div class="stat-card" title="Refreshes automatically the next time you open the app after this counts down - not a background job, so it needs a visit to trigger.">
          <div class="stat-label">New trading day in</div>
          <div class="stat-value gold" id="refresh-countdown-value">--:--:--</div>
        </div>
      </div>

      <div class="news-results-grid">
      <div class="card">
        <div class="card-header">
          <h2>📰 Latest News</h2>
          <span class="live-tag" id="news-updated-at">Loading…</span>
        </div>
        <div class="news-hint">
          Plain-English notes on what each headline could mean for the market - based on simple
          keyword rules, not real analysis. Always verify before acting on anything here.
        </div>
        <div class="news-section-label">🇮🇳 Indian Market</div>
        <div id="market-news-list"><div class="empty">Loading market news…</div></div>
        <div class="news-section-label" style="margin-top:18px">🌍 World News</div>
        <div id="world-news-list"><div class="empty">Loading world news…</div></div>
      </div>

      <div class="card results-card">
        <div class="card-header">
          <h2>📊 Quarterly Results</h2>
          <span class="live-tag" id="results-updated-at">Loading…</span>
        </div>
        <div class="news-hint">
          Results filed this month and board meetings scheduled to consider results (NSE-listed
          companies). Dates can change - confirm on the exchange. Tap ☆ to shortlist a stock. The buy/wait notes are simple rules (trend, RSI, news keywords) on the last close - not advice and not based on the actual results.
        </div>
        <div class="results-tabs">
          <button class="results-tab active" id="rtab-upcoming" onclick="switchResultsTab('upcoming')">Upcoming <span id="results-upcoming-count"></span></button>
          <button class="results-tab" id="rtab-published" onclick="switchResultsTab('published')">Published this month <span id="results-published-count"></span></button>
          <button class="results-tab" id="rtab-shortlist" onclick="switchResultsTab('shortlist')">★ Shortlist <span id="results-shortlist-count"></span></button>
        </div>
        <input type="text" id="results-filter" class="results-filter" placeholder="Filter by company or symbol" oninput="renderResults()" autocomplete="off">
        <div id="results-list" class="results-list"><div class="empty">Loading results calendar…</div></div>
      </div>
      </div>

      <div class="ipo-section-title">🔍 Search &amp; track a stock</div>
      <div class="search-box">
        <input type="text" id="stock-search-input" placeholder="Search any NSE/BSE stock - e.g. RELIANCE, TCS, or Tata Motors" autocomplete="off">
        <button id="stock-search-btn" onclick="searchStock()">Search</button>
      </div>
      <div class="search-hint">Works for any listed stock, not just the daily-scan watchlist. Star a result to add it below with Intraday, Short Term and Long Term details.</div>
      <div id="search-result"></div>

      <div id="dashboard-signals-preview"></div>

      <div id="my-selections"></div>
    </div>

    <!-- ===================== IPO TAB ===================== -->
    <div id="tab-ipo" class="tab-panel">
      <div class="meta" id="ipo-updated-at" style="margin-bottom:4px">Loading live IPO calendar…</div>
      <div class="search-hint" style="margin-bottom:14px">
        Dates, price band and subscription are pulled live from NSE. GMP has no free/official
        source anywhere - enter it yourself from wherever you already check it, and this
        auto-calculates the estimated listing gain and verdict.
      </div>
      <div id="ipo-dashboard-content"></div>
    </div>

    <!-- ===================== DIVIDENDS TAB ===================== -->
    <div id="tab-dividends" class="tab-panel">
      <div class="meta" id="dividend-updated-at" style="margin-bottom:4px">Loading upcoming dividend picks…</div>
      <div class="search-hint" style="margin-bottom:14px">
        Watchlist stocks going ex-dividend at least 14 days from now - enough runway to actually
        act on it - paired with the same Entry/Stoploss/Target rule engine used everywhere else.
        Ex-dates and amounts are live from NSE, so this refreshes on its own - no "Generate report" needed.
      </div>
      <div id="dividend-content"></div>
    </div>

    <!-- ===================== FUNDAMENTALS TAB ===================== -->
    <div id="tab-fundamentals" class="tab-panel">
      <div class="meta" id="fund-updated-at" style="margin-bottom:4px">Loading fundamentally strong stocks…</div>
      <div class="search-hint" style="margin-bottom:14px">
        Liquid NSE stocks that pass a simple quality screen: return on equity, debt, revenue and profit growth,
        margins, P/E and size (Yahoo Finance data, re-checked weekly). Entry / SL / Target use the same long-term
        rule as the Signals tab; when the chart isn't in an uptrend yet, Entry is the level to wait for. The
        timeline is a rule of thumb from how far the target is. A screen can't see governance, pledging or sector
        risk - do your own research; this is not investment advice.
      </div>
      <input type="text" id="fund-filter" class="results-filter" placeholder="Filter by company, symbol or sector" oninput="renderFundamentals()" autocomplete="off">
      <div id="fund-content"><div class="empty">Loading…</div></div>
    </div>

    <!-- ===================== SIGNALS TAB ===================== -->
    <div id="tab-signals" class="tab-panel">
      <div id="report-content"></div>
    </div>

    <div class="disclaimer">
      <strong>Entry/Stoploss/Target levels are calculated from the most recent completed trading day's
      data</strong> (not live intraday prices) - they're reference levels to watch against the live price
      once the market opens, not a guarantee the live price matches exactly. All signals shown are
      long/buy-side setups only. These are rule-based technical setups calculated from real price data,
      not predictions or guarantees. You decide whether to act on any signal. IPO GMP is an unofficial,
      informal market indicator. Alerts only fire while this tab is open in your browser. The watchlist
      chart and prices use the same delayed free data source described below each result - not an
      exchange-certified real-time feed.
    </div>

  </main>
</div>

<script>
  // --- Theme (light/dark) ---
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = theme === 'light' ? '☀️' : '🌙';
  }
  function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = current === 'light' ? 'dark' : 'light';
    applyTheme(next);
    try { localStorage.setItem('daily-brief-theme', next); } catch (e) {}
    if (watchlistChart) renderWatchlist();
  }
  (function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem('daily-brief-theme'); } catch (e) {}
    if (saved === 'light' || saved === 'dark') {
      applyTheme(saved);
    } else {
      const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
      applyTheme(prefersLight ? 'light' : 'dark');
    }
  })();

  document.getElementById('today-date').textContent =
    new Date().toLocaleDateString('en-IN', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

  function updateCountdown() {
    const now = new Date();
    const open = new Date();
    open.setHours(9, 15, 0, 0);
    if (now > open) open.setDate(open.getDate() + 1);
    const diff = open - now;
    const h = String(Math.floor(diff / 3600000)).padStart(2, '0');
    const m = String(Math.floor((diff % 3600000) / 60000)).padStart(2, '0');
    const s = String(Math.floor((diff % 60000) / 1000)).padStart(2, '0');
    document.getElementById('countdown-value').textContent = `${h}:${m}:${s}`;
  }
  updateCountdown();
  setInterval(updateCountdown, 1000);

  // Counts down to the START of the next calendar day (local time, assumed
  // IST) - the point at which today's cached report becomes stale and
  // eligible for auto-refresh. This does NOT fire a refresh by itself (no
  // background job runs here) - it just tells you when your next visit
  // will trigger one, same idea as the "Market opens in" clock beside it.
  function updateRefreshCountdown() {
    const now = new Date();
    const midnight = new Date();
    midnight.setHours(24, 0, 0, 0);
    const diff = midnight - now;
    const h = String(Math.floor(diff / 3600000)).padStart(2, '0');
    const m = String(Math.floor((diff % 3600000) / 60000)).padStart(2, '0');
    const s = String(Math.floor((diff % 60000) / 1000)).padStart(2, '0');
    const el = document.getElementById('refresh-countdown-value');
    if (el) el.textContent = `${h}:${m}:${s}`;
  }
  updateRefreshCountdown();
  setInterval(updateRefreshCountdown, 1000);

  let currentReportData = null;
  let scanRunning = false;

  // 15-minute refresh cycle: re-pulls news, the results calendar, the buy/wait
  // suggestions and the daily-report check. (Live prices already poll every 60s on
  // their own; the heavy full-market scan stays once per trading day.) The label is
  // computed from timestamps, so it stays correct even if the tab was in the background.
  const REFRESH_INTERVAL_MS = 15 * 60 * 1000;
  let nextRefreshAt = Date.now() + REFRESH_INTERVAL_MS;
  let lastRefreshedAt = Date.now();
  let refreshingNow = false;

  // Small "what was refreshed" line under the header. It records when each part was last
  // CHECKED - if a source is down the part keeps its old data and says so on its own card.
  const refreshLog = {};
  function renderRefreshLog() {
    const el = document.getElementById('refresh-log');
    if (!el) return;
    const parts = Object.entries(refreshLog).map(([k, t]) => `${k} ${fmtClock(t)}`);
    el.textContent = parts.length ? 'Last refresh — ' + parts.join(' · ') : '';
  }
  async function trackRefresh(name, fn) {
    try { await fn(); } finally { refreshLog[name] = Date.now(); renderRefreshLog(); }
  }
  function fmtClock(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  function updateRefreshEta() {
    const el = document.getElementById('refresh-eta');
    if (!el) return;
    if (scanRunning) { el.textContent = 'Scanning now…'; return; }
    if (refreshingNow) { el.textContent = 'Refreshing…'; return; }
    const diff = nextRefreshAt - Date.now();
    if (diff <= 0) { runPeriodicRefresh(); return; }
    const m = String(Math.floor(diff / 60000)).padStart(2, '0');
    const s = String(Math.floor((diff % 60000) / 1000)).padStart(2, '0');
    el.textContent = `Updated ${fmtClock(lastRefreshedAt)} · next refresh in ${m}:${s}`;
  }
  async function runPeriodicRefresh() {
    if (refreshingNow) return;
    refreshingNow = true;
    updateRefreshEta();
    try {
      await Promise.race([new Promise(r => setTimeout(r, 120000)), Promise.allSettled([
        trackRefresh('News', () => loadNewsDigest(true)),
        trackRefresh('Stocks', async () => { await renderWatchlist(); await refreshSignalLivePrices(); await refreshFundamentalPrices(); }),
        trackRefresh('Results', () => loadResultsCalendar(true)),
        trackRefresh('Daily report', () => autoRefreshReport()),
        (fundamentalsLoadedOnce ? trackRefresh('Fundamentals', () => loadFundamentalPicks()) : null),
        (typeof ipoLoadedOnce !== 'undefined' && ipoLoadedOnce) ? loadIpoDashboard() : null,
        (typeof dividendsLoadedOnce !== 'undefined' && dividendsLoadedOnce) ? loadDividendPicks() : null,
      ])]);
    } finally {
      lastRefreshedAt = Date.now();
      nextRefreshAt = lastRefreshedAt + REFRESH_INTERVAL_MS;
      refreshingNow = false;
      updateRefreshEta();
    }
  }
  updateRefreshEta();
  setInterval(updateRefreshEta, 1000);

  // --- Zerodha Kite Connect status (genuine real-time vs Yahoo fallback) ---
  function sourceBadge(source) {
    if (source === 'groww') return '<span class="src-badge src-kite">🟢 Real-time (Groww)</span>';
    if (source === 'kite') return '<span class="src-badge src-kite">🟢 Real-time (Zerodha)</span>';
    if (source === 'yahoo') return '<span class="src-badge src-yahoo">🟡 Delayed</span>';
    return '';
  }
  // Re-prices every signal/search card on screen. The "Live" figure on a card is otherwise
  // frozen at the moment the report was generated (it could be hours old), so this runs when
  // a report is shown and on every 15-minute refresh.
  async function refreshSignalLivePrices() {
    const rows = [...document.querySelectorAll('.live-price-row[data-live-symbol]')].filter(r => r.dataset.liveSymbol);
    const symbols = [...new Set(rows.map(r => r.dataset.liveSymbol))];
    for (let i = 0; i < symbols.length; i += 40) {
      try {
        const res = await fetch('/api/live-price?symbols=' + encodeURIComponent(symbols.slice(i, i + 40).join(',')));
        const json = await res.json();
        if (json.status !== 'ok') continue;
        const prices = json.prices || {};
        const sources = json.sources || {};
        rows.forEach(r => {
          const live = prices[r.dataset.liveSymbol];
          const entry = parseFloat(r.dataset.entry);
          if (live === undefined || live === null || !entry) return;
          r.innerHTML = liveRowInner(live, entry, sources[r.dataset.liveSymbol]);
        });
        if (currentReportData && currentReportData.signals) {
          currentReportData.signals.forEach(sig => {
            const v = prices[sig.symbol];
            if (v !== undefined && v !== null) { sig.live_price = v; sig.live_price_source = sources[sig.symbol]; }
          });
        }
      } catch (e) { /* keep the last shown price */ }
    }
  }

  async function refreshKiteStatus() {
    const el = document.getElementById('kite-status');
    if (!el) return;
    try {
      const res = await fetch('/api/data-status');
      const s = await res.json();
      const groww = s.groww || {};
      const kite = s.kite || {};
      if (groww.connected) {
        el.innerHTML = '<span class="dot on"></span>Groww connected (auto real-time)';
      } else if (kite.connected) {
        el.innerHTML = '<span class="dot on"></span>Zerodha connected';
      } else if (groww.configured) {
        el.innerHTML = '<span class="dot off"></span>Groww auth failed - check API key/TOTP secret';
      } else if (kite.configured) {
        el.innerHTML = '<span class="dot off"></span><a href="/kite/login">Connect Zerodha</a> for real-time';
      } else {
        el.innerHTML = '<span class="dot off"></span>No broker connected<br>(using delayed data)';
      }
    } catch (e) {
      el.textContent = '';
    }
  }
  refreshKiteStatus();
  setInterval(refreshKiteStatus, 5 * 60000);

  // --- Dashboard tabs ---
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  function switchTab(name) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('tab-' + name);
    if (panel) panel.classList.add('active');
    const navBtn = document.querySelector(`.nav-item[data-tab="${name}"]`);
    if (navBtn) navBtn.classList.add('active');
    const titleMap = { dashboard: 'Dashboard', signals: 'Signals', ipo: 'IPO Watch', dividends: 'Dividends', fundamentals: 'Fundamentally Strong Stocks' };
    const h1 = document.querySelector('.topbar h1');
    if (h1) h1.textContent = titleMap[name] || 'Dashboard';
    if (name === 'ipo' && !ipoLoadedOnce) { ipoLoadedOnce = true; loadIpoDashboard(); }
    if (name === 'dividends' && !dividendsLoadedOnce) { dividendsLoadedOnce = true; loadDividendPicks(); }
    if (name === 'fundamentals' && !fundamentalsLoadedOnce) { fundamentalsLoadedOnce = true; loadFundamentalPicks(); }
  }
  let ipoLoadedOnce = false;
  let dividendsLoadedOnce = false;
  let fundamentalsLoadedOnce = false;

  // --- Watchlist chart + panel (driven by the same ★ Track this selections) ---
  let watchlistChart = null;
  const historyCache = {}; // symbol -> [{date, close}], fetched once per symbol per page load

  function getTrackedSignals() {
    const selKeys = getSelections();
    const fromReport = currentReportData
      ? (currentReportData.signals || []).filter(s => selKeys.includes(selectionKey(s)))
      : [];
    const reportKeys = new Set(fromReport.map(selectionKey));
    const custom = getCustomTracked();
    const extra = selKeys.filter(k => !reportKeys.has(k) && custom[k]).map(k => custom[k]);
    return [...fromReport, ...extra];
  }

  async function ensureHistory(symbols) {
    const missing = symbols.filter(sym => !historyCache[sym]);
    if (missing.length === 0) return;
    try {
      const res = await fetch('/api/history?symbols=' + encodeURIComponent(missing.join(',')));
      const json = await res.json();
      if (json.status === 'ok') Object.assign(historyCache, json.history);
    } catch (e) {
      console.error('Watchlist history fetch failed', e);
    }
  }

  const CHART_COLORS = ['#d4a94f', '#4caf7d', '#7aa2d9', '#d96b62', '#b98fd1', '#5fc7c1'];

  async function renderWatchlist() {
    const tracked = getTrackedSignals();
    const bySymbol = {};
    tracked.forEach(s => { if (!bySymbol[s.symbol]) bySymbol[s.symbol] = s; });
    const symbols = Object.keys(bySymbol);

    const statTracked = document.getElementById('stat-tracked');
    if (statTracked) statTracked.textContent = symbols.length;

    const listEl = document.getElementById('watchlist-list');
    const emptyState = document.getElementById('chart-empty-state');
    const canvas = document.getElementById('watchlist-chart');
    if (!listEl || !canvas) return;

    if (symbols.length === 0) {
      listEl.innerHTML = '<div class="empty">No stocks tracked yet.</div>';
      emptyState.style.display = 'block';
      canvas.style.display = 'none';
      if (watchlistChart) { watchlistChart.destroy(); watchlistChart = null; }
      return;
    }

    emptyState.style.display = 'none';
    canvas.style.display = 'block';

    await ensureHistory(symbols);

    let livePrices = {};
    let liveSources = {};
    try {
      const res = await fetch('/api/live-price?symbols=' + encodeURIComponent(symbols.join(',')));
      const json = await res.json();
      if (json.status === 'ok') {
        livePrices = json.prices || {};
        liveSources = json.sources || {};
      }
    } catch (e) { /* fall back to last known close below */ }

    listEl.innerHTML = '';
    symbols.forEach(sym => {
      const s = bySymbol[sym];
      const hist = historyCache[sym] || [];
      const prevClose = hist.length > 0 ? hist[hist.length - 1].close : null;
      const live = livePrices[sym] !== undefined ? livePrices[sym] : prevClose;
      const changePct = (prevClose && live !== null && live !== undefined) ? ((live - prevClose) / prevClose * 100) : null;
      const changeColor = changePct === null ? 'var(--text-muted)' : (changePct >= 0 ? 'var(--green)' : 'var(--red)');
      const changeLabel = changePct === null ? '—' : `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`;
      const row = document.createElement('div');
      row.className = 'watchlist-row';
      row.innerHTML = `
        <div><div class="wl-name">${s.symbol_short}</div><div class="wl-style">${s.style}</div></div>
        <div style="display:flex;align-items:center">
          <div><div class="wl-price">${live !== null && live !== undefined ? '₹' + live : '—'}${sourceBadge(liveSources[sym])}</div><div class="wl-change" style="color:${changeColor}">${changeLabel}</div></div>
          <button class="wl-remove-btn" title="Remove from watchlist">★</button>
        </div>
      `;
      row.querySelector('.wl-remove-btn').addEventListener('click', () => untrackSymbol(sym));
      listEl.appendChild(row);
    });

    const labels = (historyCache[symbols[0]] || []).map(p => p.date);
    labels.push('Now');

    const datasets = symbols.map((sym, i) => {
      const hist = historyCache[sym] || [];
      const data = hist.map(p => p.close);
      const live = livePrices[sym] !== undefined ? livePrices[sym] : (hist.length ? hist[hist.length - 1].close : null);
      data.push(live);
      const color = CHART_COLORS[i % CHART_COLORS.length];
      return {
        label: bySymbol[sym].symbol_short,
        data,
        borderColor: color,
        backgroundColor: color + '22',
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      };
    });

    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const gridColor = isLight ? '#dadfe8' : '#262e3d';
    const tickColor = isLight ? '#5b6474' : '#8a93a3';

    if (watchlistChart) {
      watchlistChart.data.labels = labels;
      watchlistChart.data.datasets = datasets;
      watchlistChart.options.scales.x.grid.color = gridColor;
      watchlistChart.options.scales.y.grid.color = gridColor;
      watchlistChart.options.scales.x.ticks.color = tickColor;
      watchlistChart.options.scales.y.ticks.color = tickColor;
      watchlistChart.options.plugins.legend.labels.color = tickColor;
      watchlistChart.update();
    } else if (window.Chart) {
      watchlistChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: { labels, datasets },
        options: {
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: { legend: { labels: { color: tickColor, boxWidth: 10, padding: 12 } } },
          scales: {
            x: { grid: { color: gridColor }, ticks: { color: tickColor, maxTicksLimit: 8 } },
            y: { grid: { color: gridColor }, ticks: { color: tickColor } },
          },
        },
      });
    }
  }

  // Keep the watchlist panel/chart's live prices fresh even without alerts enabled.
  setInterval(renderWatchlist, 60000);

  // --- Selections (localStorage-backed "going to invest" shortlist) ---
  // Regular signals (from the daily scan) are re-resolved from currentReportData
  // each render. Stocks starred from Search - which may not be in that day's
  // scan at all - need their own snapshot saved, so they still show up in
  // My Suggestions / the watchlist chart even without a matching daily signal.
  function selectionKey(s) { return s.symbol + '|' + s.style; }
  function getSelections() {
    try {
      return JSON.parse(localStorage.getItem('daily-brief-selections') || '[]');
    } catch (e) { return []; }
  }
  function getCustomTracked() {
    try { return JSON.parse(localStorage.getItem('daily-brief-custom-tracked') || '{}'); } catch (e) { return {}; }
  }
  function saveSelections(sel) {
    try { localStorage.setItem('daily-brief-selections', JSON.stringify(sel)); } catch (e) {}
  }
  function saveCustomTracked(custom) {
    try { localStorage.setItem('daily-brief-custom-tracked', JSON.stringify(custom)); } catch (e) {}
  }
  function isSelected(s) { return getSelections().includes(selectionKey(s)); }

  function refreshAllTrackedViews() {
    if (currentReportData) renderReport(currentReportData);
    else { renderMySelections(); renderWatchlist(); }
  }

  function untrackSymbol(symbol) {
    const sel = getSelections().filter(k => !k.startsWith(symbol + '|'));
    const custom = getCustomTracked();
    Object.keys(custom).forEach(k => { if (k.startsWith(symbol + '|')) delete custom[k]; });
    saveSelections(sel);
    saveCustomTracked(custom);
    refreshAllTrackedViews();
  }
  function toggleSelection(key) {
    let sel = getSelections();
    const custom = getCustomTracked();
    if (sel.includes(key)) {
      sel = sel.filter(k => k !== key);
      delete custom[key];
    } else {
      sel.push(key);
    }
    saveSelections(sel);
    saveCustomTracked(custom);
    refreshAllTrackedViews();
  }
  // Used when starring a stock from Search: tracks ALL of its timeframes
  // (Intraday/Short Term/Long Term) at once with a single star click, since
  // the stock may not be part of today's daily scan and its data needs to
  // be saved directly, not just referenced by key. Untracking removes every
  // timeframe for that symbol together too.
  function toggleStockTrack(data) {
    const trackableTfs = (data.timeframes || []).filter(tf => tf.has_setup);
    const anyTracked = trackableTfs.some(tf => isSelected({ symbol: tf.symbol, style: tf.style }));

    if (anyTracked) {
      untrackSymbol(data.symbol);
    } else {
      let sel = getSelections();
      const custom = getCustomTracked();
      trackableTfs.forEach(tf => {
        const merged = {
          symbol: tf.symbol, symbol_short: data.symbol_short, style: tf.style,
          entry: tf.entry, stoploss: tf.stoploss, target: tf.target, risk_reward: tf.risk_reward,
          live_price: data.live_price, live_price_source: data.live_price_source,
          news: data.news || [], direction: 'Bullish', reason: tf.reason,
        };
        const key = selectionKey(merged);
        if (!sel.includes(key)) sel.push(key);
        custom[key] = merged;
      });
      saveSelections(sel);
      saveCustomTracked(custom);
      refreshAllTrackedViews();
    }
    if (lastSearchData) renderSearchResult(lastSearchData);
  }

  // --- My average price (personal note, per stock - localStorage only) ---
  function getAvgPrices() {
    try { return JSON.parse(localStorage.getItem('daily-brief-avg-prices') || '{}'); } catch (e) { return {}; }
  }
  function saveAvgPrice(symbol, price) {
    const prices = getAvgPrices();
    prices[symbol] = price;
    try { localStorage.setItem('daily-brief-avg-prices', JSON.stringify(prices)); } catch (e) {}
    refreshAllTrackedViews();
  }

  function liveRowInner(live, entry, source) {
    const diffPct = ((live - entry) / entry * 100).toFixed(1);
    const diffLabel = diffPct >= 0 ? `+${diffPct}%` : `${diffPct}%`;
    const diffColor = live >= entry ? 'var(--green)' : 'var(--red)';
    return `📡 Live: <strong>${live}</strong> <span style="color:${diffColor}">(${diffLabel} vs entry)</span> ${sourceBadge(source)}`;
  }

  function buildFiguresHtml(s) {
    let liveHtml = '';
    const hasLive = s.live_price !== undefined && s.live_price !== null;
    if (hasLive || s.symbol) {
      liveHtml = `<div class="live-price-row" data-live-symbol="${s.symbol || ''}" data-entry="${s.entry}">` +
        (hasLive ? liveRowInner(s.live_price, s.entry, s.live_price_source)
                 : '📡 Live: <span style="color:var(--text-muted)">updating…</span>') + '</div>';
    }
    const targetPct = ((s.target - s.entry) / s.entry * 100).toFixed(1);
    const slPct = ((s.stoploss - s.entry) / s.entry * 100).toFixed(1);
    return `
      <div class="figures">
        <div><span class="flabel">📍 Entry</span>${s.entry}</div>
        <div><span class="flabel">🛑 Stoploss</span>${s.stoploss} <span class="pct-tag" style="color:var(--red)">(${slPct}%)</span></div>
        <div><span class="flabel">🎯 Target</span>${s.target} <span class="pct-tag" style="color:var(--green)">(+${targetPct}%)</span></div>
      </div>
      ${liveHtml}`;
  }

  function directionInfo(s) {
    const known = (s.direction === 'Bullish' || s.direction === 'Bearish') ? s.direction : null;
    if (known === 'Bearish') {
      return { cls: 'direction-bearish', badge: '<span class="badge dir-bearish">▼ Bearish - caution</span>' };
    } else if (known === 'Bullish') {
      return { cls: 'direction-bullish', badge: '<span class="badge dir-bullish">▲ Bullish</span>' };
    }
    return { cls: '', badge: '<span class="badge dir-unknown">⚠ Direction unavailable - verify manually</span>' };
  }

  function buildRow(s) {
    const row = document.createElement('div');
    const dir = directionInfo(s);
    row.className = 'row ' + dir.cls;
    const key = selectionKey(s);
    const selected = isSelected(s);
    let newsHtml = '';
    if (s.news && s.news.length > 0) {
      newsHtml = '<div class="news">' + s.news.map(n =>
        `<a href="${n.link}" target="_blank">${n.title}</a>`
      ).join('<br>') + '</div>';
    }
    const cautionHtml = s.caution ? `<div class="caution-banner">⚠ ${s.caution}</div>` : '';

    let avgPriceHtml = '';
    if (selected) {
      const avgPrice = getAvgPrices()[s.symbol];
      let pnlHtml = '';
      if (avgPrice && s.live_price !== undefined && s.live_price !== null) {
        const pnlPct = ((s.live_price - avgPrice) / avgPrice * 100).toFixed(1);
        const pnlColor = pnlPct >= 0 ? 'var(--green)' : 'var(--red)';
        pnlHtml = `<span class="pnl-tag" style="color:${pnlColor}">${pnlPct >= 0 ? '+' : ''}${pnlPct}% P&amp;L</span>`;
      }
      avgPriceHtml = `
        <div class="avg-price-row">
          <span class="flabel">My avg. price</span>
          <input type="number" step="0.01" class="avg-price-input" value="${avgPrice || ''}" placeholder="₹0.00">
          <button class="link-btn avg-save-btn">Save</button>
          ${pnlHtml}
        </div>`;
    }

    row.innerHTML = `
      <div class="name">
        <span>${s.symbol_short} ${dir.badge}</span>
        <button class="star-btn ${selected ? 'selected' : ''}" data-key="${key}">${selected ? '★ Tracking' : '☆ Track this'}</button>
      </div>
      ${buildFiguresHtml(s)}
      ${avgPriceHtml}
      ${cautionHtml}
      <div class="reason">${s.reason}</div>
      ${newsHtml}
    `;
    row.querySelector('.star-btn').addEventListener('click', () => toggleSelection(key));
    const avgBtn = row.querySelector('.avg-save-btn');
    if (avgBtn) {
      avgBtn.addEventListener('click', () => {
        const input = row.querySelector('.avg-price-input');
        const val = parseFloat(input.value);
        if (!isNaN(val) && val > 0) saveAvgPrice(s.symbol, val);
      });
    }
    return row;
  }

  function renderMySelections() {
    const wrap = document.getElementById('my-selections');
    const tracked = getTrackedSignals();
    if (tracked.length === 0) {
      wrap.innerHTML = '';
      return;
    }
    wrap.innerHTML = `<h2>My selections <span class="count">(${tracked.length})</span></h2>`;
    const rowsWrap = document.createElement('div');
    rowsWrap.className = 'rows';
    tracked.forEach(s => rowsWrap.appendChild(buildRow(s)));
    wrap.appendChild(rowsWrap);
  }

  function verdictClass(verdict) {
    if (!verdict) return '';
    const v = verdict.toLowerCase();
    if (v.includes('strong')) return 'verdict-strong';
    if (v.includes('avoid')) return 'verdict-avoid';
    if (v.includes('moderate')) return 'verdict-moderate';
    if (v.includes('weak')) return 'verdict-weak';
    if (v.includes('caution')) return 'verdict-caution';
    return '';
  }

  // --- IPO Watch tab: live NSE calendar + manually-entered GMP ---
  function buildIpoCard(ipo) {
    const card = document.createElement('div');
    card.className = 'ipo-card';
    const dateRange = ipo.open_date && ipo.close_date ? `${ipo.open_date} - ${ipo.close_date}` : (ipo.open_date || '');
    let gmpHtml = '';
    if (ipo.gmp !== null && ipo.gmp !== undefined) {
      gmpHtml = `
        <span class="verdict-badge ${verdictClass(ipo.verdict)}">${ipo.verdict}</span>
        GMP: ₹${ipo.gmp} · Est. listing ₹${ipo.est_listing} (${ipo.gain_pct >= 0 ? '+' : ''}${ipo.gain_pct}%)
        <button class="link-btn" onclick="showGmpForm(this)">Edit</button>
        <div class="gmp-form" style="display:none;margin-top:8px">
          <input type="number" step="0.01" placeholder="New GMP (₹)" value="${ipo.gmp}">
          <button onclick="saveGmp(this, '${ipo.symbol}')">Save</button>
        </div>`;
    } else {
      gmpHtml = `
        <div class="gmp-form">
          <input type="number" step="0.01" placeholder="Enter GMP (₹)">
          <button onclick="saveGmp(this, '${ipo.symbol}')">Save</button>
        </div>`;
    }
    card.innerHTML = `
      <div class="ipo-card-header">
        <div class="ipo-name">${ipo.name} <span class="badge">${ipo.segment}</span></div>
        <div class="ipo-dates">${dateRange}${ipo.status ? ' · ' + ipo.status : ''}</div>
      </div>
      <div class="ipo-figures">
        <div><span class="flabel">Price band</span>${ipo.issue_price_range || '—'}</div>
        <div><span class="flabel">Subscription</span>${ipo.subscription_times !== null ? ipo.subscription_times + 'x' : '—'}</div>
        <div><span class="flabel">Issue size</span>${ipo.issue_size || '—'}</div>
      </div>
      <div class="ipo-gmp-row">${gmpHtml}</div>
    `;
    return card;
  }

  function showGmpForm(btn) {
    const form = btn.parentElement.querySelector('.gmp-form');
    if (form) form.style.display = form.style.display === 'none' ? 'flex' : 'none';
  }

  async function saveGmp(btn, symbol) {
    const input = btn.previousElementSibling;
    const gmp = parseFloat(input.value);
    if (isNaN(gmp)) { alert('Enter a valid GMP number.'); return; }
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      const res = await fetch('/api/ipo-gmp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, gmp }),
      });
      const json = await res.json();
      if (json.status === 'ok') {
        renderIpoDashboard(json.data);
      } else {
        alert(json.message || 'Could not save GMP.');
        btn.disabled = false;
        btn.textContent = 'Save';
      }
    } catch (e) {
      alert('Could not save GMP. Try again.');
      btn.disabled = false;
      btn.textContent = 'Save';
    }
  }

  function renderIpoDashboard(data) {
    const wrap = document.getElementById('ipo-dashboard-content');
    wrap.innerHTML = '';

    const sections = [
      { key: 'current', title: '🟢 Live & Applicable' },
      { key: 'upcoming', title: '📅 Upcoming' },
      { key: 'recent', title: '📈 Recently Listed' },
    ];

    let anyIpos = false;
    sections.forEach(({ key, title }) => {
      const list = data[key] || [];
      if (list.length === 0) return;
      anyIpos = true;

      const heading = document.createElement('div');
      heading.className = 'ipo-section-title';
      heading.textContent = `${title} (${list.length})`;
      wrap.appendChild(heading);

      if (key === 'recent') {
        list.forEach(ipo => {
          const row = document.createElement('div');
          row.className = 'recent-ipo-row';
          row.innerHTML = `
            <div><strong>${ipo.name}</strong> <span class="badge">${ipo.segment}</span><br>
              <span class="flabel">Price band ${ipo.issue_price_range || '—'} · Sub ${ipo.subscription_times !== null ? ipo.subscription_times + 'x' : '—'}</span>
            </div>
            <button class="link-btn" onclick="checkListedPrice('${ipo.symbol}')">Check current price →</button>
          `;
          wrap.appendChild(row);
        });
      } else {
        const grid = document.createElement('div');
        grid.className = 'ipo-grid';
        list.forEach(ipo => grid.appendChild(buildIpoCard(ipo)));
        wrap.appendChild(grid);
      }
    });

    if (!anyIpos) {
      wrap.innerHTML = '<div class="empty">No IPO data available right now - NSE\'s feed may be temporarily unavailable. Try refreshing shortly.</div>';
    }
  }

  function checkListedPrice(symbol) {
    switchTab('dashboard');
    document.getElementById('stock-search-input').value = symbol;
    runSearch(symbol);
  }

  async function loadIpoDashboard() {
    const statusEl = document.getElementById('ipo-updated-at');
    statusEl.textContent = 'Loading live IPO calendar…';
    try {
      const res = await fetch('/api/ipo-dashboard');
      const json = await res.json();
      if (json.status === 'ok') {
        renderIpoDashboard(json.data);
        statusEl.textContent = 'Live from NSE · refreshed every ~10 min';
      } else {
        statusEl.textContent = 'Could not load IPO data.';
      }
    } catch (e) {
      statusEl.textContent = 'Could not load IPO data. Check your connection and try again.';
    }
  }

  // --- Latest News: market + world headlines with a simple impact note ---
  function buildNewsItem(item) {
    const div = document.createElement('div');
    div.className = 'news-item';
    div.innerHTML = `
      <div class="news-title"><a href="${item.link}" target="_blank" rel="noopener">${item.title}</a></div>
      <div class="news-impact"><span class="impact-icon">💡</span>${item.impact}</div>
    `;
    return div;
  }

  function renderNewsList(containerId, items) {
    const wrap = document.getElementById(containerId);
    wrap.innerHTML = '';
    if (!items || items.length === 0) {
      wrap.innerHTML = '<div class="empty">No headlines available right now - try refreshing shortly.</div>';
      return;
    }
    items.forEach(item => wrap.appendChild(buildNewsItem(item)));
  }

  async function loadNewsDigest(force) {
    const statusEl = document.getElementById('news-updated-at');
    try {
      const res = await fetch('/api/market-news' + (force ? '?refresh=1' : ''));
      const json = await res.json();
      if (json.status === 'ok') {
        renderNewsList('market-news-list', json.data.market);
        renderNewsList('world-news-list', json.data.world);
        statusEl.textContent = 'Updated ' + fmtClock(Date.now()) + ' · every 15 min';
      } else {
        statusEl.textContent = 'Could not load news.';
      }
    } catch (e) {
      statusEl.textContent = 'Could not load news. Check your connection and try again.';
    }
  }

  // --- Quarterly results: filed this month + upcoming board meetings ---
  let resultsData = null;
  let resultsTab = 'upcoming';
  let resultsShown = 15;
  const RESULTS_PAGE = 15;
  const RESULTS_SHORTLIST_KEY = 'daily-brief-results-shortlist';
  const resultsSuggCache = {};
  const resultsSuggPending = {};

  function escHtml(v) {
    return String(v === null || v === undefined ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function getResultsShortlist() {
    try { return JSON.parse(localStorage.getItem(RESULTS_SHORTLIST_KEY) || '{}'); } catch (e) { return {}; }
  }
  function saveResultsShortlist(sl) {
    try { localStorage.setItem(RESULTS_SHORTLIST_KEY, JSON.stringify(sl)); } catch (e) {}
  }
  function toggleResultsShortlist(symbol, name) {
    const sl = getResultsShortlist();
    if (sl[symbol]) delete sl[symbol]; else sl[symbol] = { symbol: symbol, name: name };
    saveResultsShortlist(sl);
    renderResults(true);
  }
  function suggKey(r) { return r.symbol + '|' + r.date + '|' + r.status; }

  function switchResultsTab(tab) {
    resultsTab = tab;
    resultsShown = RESULTS_PAGE;
    ['upcoming', 'published', 'shortlist'].forEach(t =>
      document.getElementById('rtab-' + t).classList.toggle('active', t === tab));
    renderResults();
  }

  function rowsForTab() {
    if (resultsTab !== 'shortlist') return resultsData[resultsTab] || [];
    return Object.values(getResultsShortlist()).map(s => {
      const hit = resultsData.upcoming.find(r => r.symbol === s.symbol) || resultsData.published.find(r => r.symbol === s.symbol);
      if (hit) return hit;
      return { symbol: s.symbol, name: s.name, date: '', date_display: '', weekday: '', quarter: '',
               kind: 'No result date in the current window', link: '', in_nifty100: false, status: 'published' };
    });
  }

  function suggestionHtml(sg) {
    if (!sg) return '<div class="res-sugg">Analysing…</div>';
    let h = `<div class="res-sugg sugg-${escHtml(sg.verdict_class)}">`;
    h += `<div><span class="sugg-verdict">${escHtml(sg.verdict)}</span>${escHtml(sg.summary)}</div>`;
    (sg.notes || []).forEach(n => { h += `<div class="sugg-line">• ${escHtml(n)}</div>`; });
    if (sg.snapshot) {
      const s = sg.snapshot;
      h += `<div class="sugg-line">Last close ₹${escHtml(s.close)} (${s.day_change_pct >= 0 ? '+' : ''}${escHtml(s.day_change_pct)}% that day)` +
           (s.rsi !== null ? ` · RSI ${escHtml(s.rsi)}` : '') + ` · ${escHtml(s.trend)}</div>`;
    }
    (sg.setups || []).forEach(t => {
      h += `<div class="sugg-levels"><b>${escHtml(t.label)}</b>: Entry ₹${escHtml(t.entry)} · SL ₹${escHtml(t.stoploss)} · Target ₹${escHtml(t.target)} (R:R 1:${escHtml(t.risk_reward)})</div>`;
    });
    if ((sg.news || []).length) {
      h += '<div class="sugg-line sugg-news">News: ' + sg.news.slice(0, 2).map(n =>
        /^https?:/.test(n.link) ? `<a href="${escHtml(n.link)}" target="_blank" rel="noopener">${escHtml(n.title)}</a>` : escHtml(n.title)
      ).join(' &nbsp;|&nbsp; ') + '</div>';
    } else {
      h += '<div class="sugg-line">News: no recent headlines found.</div>';
    }
    return h + '</div>';
  }

  function paintSuggestions() {
    document.querySelectorAll('#results-list .res-sugg[data-key]').forEach(el => {
      const sg = resultsSuggCache[el.dataset.key];
      if (sg) el.outerHTML = suggestionHtml(sg).replace('<div class="res-sugg', '<div data-done="1" class="res-sugg');
    });
  }

  let lastVisibleRows = [];
  async function loadSuggestions(rows, force) {
    const need = rows.filter(r => !resultsSuggPending[suggKey(r)] && (force || !resultsSuggCache[suggKey(r)]));
    for (let i = 0; i < need.length; i += 8) {
      const batch = need.slice(i, i + 8);
      batch.forEach(r => { resultsSuggPending[suggKey(r)] = true; });
      try {
        const res = await fetch('/api/results-suggestions', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh: !!force, items: batch.map(r => ({ symbol: r.symbol, date: r.date, status: r.status })) }),
        });
        const json = await res.json();
        if (json.status === 'ok') Object.assign(resultsSuggCache, json.data);
      } catch (e) { /* leave "Analysing…" - retried on next render */ }
      batch.forEach(r => { delete resultsSuggPending[suggKey(r)]; });
      paintSuggestions();
    }
    if (force && need.length) renderResults(true);   // swap in the refreshed suggestions
  }

  function renderResults(keepPage) {
    const list = document.getElementById('results-list');
    if (!resultsData) return;
    if (!keepPage) resultsShown = RESULTS_PAGE;
    const sl = getResultsShortlist();
    document.getElementById('results-shortlist-count').textContent = '(' + Object.keys(sl).length + ')';
    const q = document.getElementById('results-filter').value.trim().toLowerCase();
    let rows = rowsForTab();
    if (q) rows = rows.filter(r => r.name.toLowerCase().includes(q) || r.symbol.toLowerCase().includes(q));
    if (rows.length === 0) {
      list.innerHTML = '<div class="empty">' + (q ? 'No matching companies.'
        : resultsTab === 'shortlist' ? 'Nothing shortlisted yet - tap ☆ beside a company to add it.'
        : resultsTab === 'upcoming' ? 'No upcoming result dates found.' : 'No results published yet this month.') + '</div>';
      return;
    }
    const visible = rows.slice(0, resultsShown);
    lastVisibleRows = visible;
    list.innerHTML = visible.map(r => {
      const nameHtml = r.link && /^https?:/.test(r.link)
        ? `<a href="${escHtml(r.link)}" target="_blank" rel="noopener">${escHtml(r.name)}</a>` : escHtml(r.name);
      const sub = [r.symbol, r.quarter, r.kind].filter(Boolean).map(escHtml).join(' · ');
      const on = !!sl[r.symbol];
      const dateHtml = r.date_display
        ? `${escHtml(r.date_display.slice(0, 6))}<small>${escHtml(r.weekday)} ${escHtml(r.date_display.slice(7))}</small>` : '—';
      const key = suggKey(r);
      return `<div class="res-item">
        <div class="res-row">
          <div class="res-date">${dateHtml}</div>
          <div class="res-main"><div class="res-name">${nameHtml}${r.in_nifty100 ? '<span class="n100-tag">N100</span>' : ''}</div><div class="res-sub">${sub}</div></div>
          <button class="res-star${on ? ' on' : ''}" data-symbol="${escHtml(r.symbol)}" data-name="${escHtml(r.name)}"
                  title="${on ? 'Remove from shortlist' : 'Add to shortlist'}">${on ? '★' : '☆'}</button>
        </div>
        ${resultsSuggCache[key] ? suggestionHtml(resultsSuggCache[key]) : `<div class="res-sugg" data-key="${escHtml(key)}">Analysing…</div>`}
      </div>`;
    }).join('') + (rows.length > resultsShown
      ? `<button class="res-more" id="res-more-btn">Show more (${rows.length - resultsShown} remaining)</button>` : '');
    loadSuggestions(visible);
  }

  document.addEventListener('click', ev => {
    const star = ev.target.closest('.res-star');
    if (star) { toggleResultsShortlist(star.dataset.symbol, star.dataset.name); return; }
    if (ev.target.id === 'res-more-btn') { resultsShown += RESULTS_PAGE; renderResults(true); }
  });

  async function loadResultsCalendar(force) {
    const statusEl = document.getElementById('results-updated-at');
    try {
      const res = await fetch('/api/results-calendar' + (force ? '?refresh=1' : ''));
      const json = await res.json();
      if (json.status !== 'ok') { statusEl.textContent = 'Could not load results.'; return; }
      resultsData = json.data;
      resultsData.upcoming.forEach(r => { r.status = 'upcoming'; });
      resultsData.published.forEach(r => { r.status = 'published'; });
      document.getElementById('results-upcoming-count').textContent = '(' + resultsData.upcoming.length + ')';
      document.getElementById('results-published-count').textContent = '(' + resultsData.published.length + ')';
      statusEl.textContent = resultsData.error ? 'Partly unavailable' : resultsData.month_label + ' · ' + fmtClock(Date.now());
      if (resultsData.error && !resultsData.upcoming.length && !resultsData.published.length) {
        document.getElementById('results-list').innerHTML =
          '<div class="empty">NSE did not respond (' + escHtml(resultsData.error) + '). It retries on the next refresh.</div>';
        return;
      }
      renderResults(true);
      if (force) await loadSuggestions(lastVisibleRows, true);
    } catch (e) {
      statusEl.textContent = 'Could not load results. Check your connection and try again.';
    }
  }

  // --- Fundamentally strong stocks ---
  let fundData = null;
  let fundPolling = false;

  function fmtRs(v) {
    return (v === null || v === undefined) ? '—' : '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }
  function fmtPct(v, mult) {
    return (v === null || v === undefined) ? '—' : (v * (mult || 1)).toFixed(0) + '%';
  }

  function renderFundamentals() {
    const box = document.getElementById('fund-content');
    if (!fundData) return;
    const q = document.getElementById('fund-filter').value.trim().toLowerCase();
    let picks = fundData.picks || [];
    if (q) picks = picks.filter(p => (p.name + ' ' + p.symbol + ' ' + p.sector).toLowerCase().includes(q));
    if (picks.length === 0) {
      box.innerHTML = '<div class="empty">' + (q ? 'No matching stocks.' : 'No stock passed the fundamentals screen in this run.') + '</div>';
      return;
    }
    box.innerHTML = '<div class="fund-grid">' + picks.map(p => {
      const m = p.metrics || {};
      const chg = p.day_change_pct;
      const chgColor = chg >= 0 ? 'var(--green)' : 'var(--red)';
      const mcap = m.market_cap ? '₹' + Math.round(m.market_cap / 1e7).toLocaleString('en-IN') + ' cr' : '—';
      const levels = p.entry === null
        ? '<div class="fund-note">No levels available - ' + escHtml(p.note) + '</div>'
        : `<div class="fund-levels">
             <div><small>Entry</small><b>${fmtRs(p.entry)}</b></div>
             <div class="sl"><small>Stop-loss</small><b>${fmtRs(p.stoploss)}</b></div>
             <div class="tg"><small>Target</small><b>${fmtRs(p.target)}</b></div>
           </div>
           <div class="fund-timeline">⏱ Timeline: ${escHtml(p.timeline)} · upside ${p.upside_pct}% · R:R 1:${escHtml(p.risk_reward)}</div>
           <div class="fund-note">${escHtml(p.note)}</div>`;
      return `<div class="card fund-card">
        <div class="fund-head">
          <div><div class="fund-name">${escHtml(p.name)}</div><div class="fund-sub">${escHtml(p.symbol_short)}${p.sector ? ' · ' + escHtml(p.sector) : ''}</div></div>
          <div class="fund-score">${escHtml(p.score)}<small>quality</small></div>
        </div>
        <div class="fund-price-row">
          <span class="fund-price" data-symbol="${escHtml(p.symbol)}" data-prev="${escHtml(p.prev_close)}">${fmtRs(p.price)}</span>
          <span class="fund-chg" data-chg-for="${escHtml(p.symbol)}" style="color:${chgColor}">${chg >= 0 ? '+' : ''}${escHtml(chg)}%</span>
          <span class="fund-status ${escHtml(p.status_class)}">${escHtml(p.status)}</span>
        </div>
        ${levels}
        <div class="fund-tags">${(p.tags || []).map(t => '<span>' + escHtml(t) + '</span>').join('')}</div>
        <div class="fund-metrics">ROE ${fmtPct(m.roe, 100)} · D/E ${m.debt_to_equity === null || m.debt_to_equity === undefined ? '—' : Number(m.debt_to_equity).toFixed(2)} · Rev ${fmtPct(m.revenue_growth, 100)} · Profit ${fmtPct(m.earnings_growth, 100)} · P/E ${m.pe ? Number(m.pe).toFixed(0) : '—'} · Mcap ${mcap}</div>
      </div>`;
    }).join('') + '</div>';
  }

  // Keeps the "current price" on each card live (Zerodha if connected, else Yahoo) without recomputing levels.
  async function refreshFundamentalPrices() {
    const els = [...document.querySelectorAll('#fund-content .fund-price[data-symbol]')];
    if (els.length === 0) return;
    const symbols = els.map(e => e.dataset.symbol);
    for (let i = 0; i < symbols.length; i += 40) {
      try {
        const res = await fetch('/api/live-price?symbols=' + encodeURIComponent(symbols.slice(i, i + 40).join(',')));
        const json = await res.json();
        if (json.status !== 'ok') continue;
        Object.entries(json.prices || {}).forEach(([sym, live]) => {
          const el = els.find(e => e.dataset.symbol === sym);
          if (!el || live === null || live === undefined) return;
          el.textContent = fmtRs(live);
          const prev = parseFloat(el.dataset.prev);
          const chgEl = document.querySelector(`#fund-content .fund-chg[data-chg-for="${CSS.escape(sym)}"]`);
          if (chgEl && prev) {
            const c = (live - prev) / prev * 100;
            chgEl.textContent = (c >= 0 ? '+' : '') + c.toFixed(2) + '%';
            chgEl.style.color = c >= 0 ? 'var(--green)' : 'var(--red)';
          }
        });
      } catch (e) { /* keep last known price */ }
    }
  }

  async function loadFundamentalPicks(isPoll) {
    if (!isPoll && fundPolling) return;          // a progress-poll is already running
    const statusEl = document.getElementById('fund-updated-at');
    try {
      const res = await fetch('/api/fundamental-picks');
      const json = await res.json();
      if (json.data) {
        fundData = json.data;
        renderFundamentals();
      }
      if (json.refreshing || json.status === 'busy') {
        const p = json.progress || {};
        statusEl.textContent = (p.total ? `Screening fundamentals: ${p.done} of ${p.total} stocks…` : 'Starting the fundamentals screen…')
          + (json.data ? ' (showing the previous results meanwhile)' : ' - the first run takes a few minutes');
        fundPolling = true;
        setTimeout(() => loadFundamentalPicks(true), 5000);
        return;
      }
      fundPolling = false;
      if (json.data) {
        statusEl.textContent = `${fundData.picks.length} strong stocks out of ${fundData.candidates_checked} liquid stocks checked · screened ${fundData.generated_at_display} · prices refresh every 15 min`;
        refreshFundamentalPrices();
      } else {
        statusEl.textContent = 'Could not load fundamentals.';
      }
    } catch (e) {
      fundPolling = false;
      statusEl.textContent = 'Could not load fundamentals. Check your connection and try again.';
    }
  }

  // --- Dividend Picks tab: live NSE ex-dividend dates + technical setups ---
  function buildDividendCard(pick) {
    const card = document.createElement('div');
    card.className = 'div-card' + (pick.has_setup ? ' has-setup' : '');
    const yieldHtml = pick.dividend_yield_pct !== null && pick.dividend_yield_pct !== undefined
      ? `${pick.dividend_yield_pct}%` : '—';
    const amountHtml = pick.dividend_amount !== null && pick.dividend_amount !== undefined
      ? `₹${pick.dividend_amount}/share` : 'amount not disclosed';

    let figuresHtml;
    if (pick.has_setup) {
      const targetPct = ((pick.target - pick.entry) / pick.entry * 100).toFixed(1);
      const slPct = ((pick.stoploss - pick.entry) / pick.entry * 100).toFixed(1);
      const timeframeLabel = pick.style === 'Swing' ? 'Short Term (1–3 weeks)' : 'Long Term (2–6 months)';
      figuresHtml = `
        <div class="figures">
          <div><span class="flabel">📍 Entry</span>${pick.entry}</div>
          <div><span class="flabel">🛑 Stoploss</span>${pick.stoploss} <span class="pct-tag" style="color:var(--red)">(${slPct}%)</span></div>
          <div><span class="flabel">🎯 Target</span>${pick.target} <span class="pct-tag" style="color:var(--green)">(+${targetPct}%)</span></div>
        </div>
        <div class="live-price-row">R:R ~1:${pick.risk_reward} · Timeframe: ${timeframeLabel}</div>`;
    } else {
      figuresHtml = `<div class="no-setup-tag">No confirmed bullish setup right now</div>`;
    }

    card.innerHTML = `
      <div class="div-card-header">
        <div><span class="div-name">${pick.name}</span> <span class="badge">${pick.symbol_short}</span></div>
        <div class="div-yield">${yieldHtml}<div class="flabel" style="text-align:right">yield</div></div>
      </div>
      <div class="div-meta">Ex-dividend ${pick.ex_date} · in ${pick.days_to_ex_date} days · ${amountHtml} · current price ₹${pick.current_price}</div>
      ${figuresHtml}
      <div class="reason">${pick.reason}</div>
    `;
    return card;
  }

  function renderDividendPicks(picks) {
    const wrap = document.getElementById('dividend-content');
    wrap.innerHTML = '';
    if (!picks || picks.length === 0) {
      wrap.innerHTML = '<div class="empty">No watchlist stocks are going ex-dividend in the next 14–90 days right now - check back later.</div>';
      return;
    }
    const grid = document.createElement('div');
    grid.className = 'ipo-grid';
    picks.forEach(p => grid.appendChild(buildDividendCard(p)));
    wrap.appendChild(grid);
  }

  async function loadDividendPicks() {
    const statusEl = document.getElementById('dividend-updated-at');
    statusEl.textContent = 'Loading upcoming dividend picks…';
    try {
      const res = await fetch('/api/dividend-picks');
      const json = await res.json();
      if (json.status === 'ok') {
        renderDividendPicks(json.data);
        statusEl.textContent = 'Live from NSE + technical scan · refreshed every ~30 min';
      } else {
        statusEl.textContent = 'Could not load dividend picks.';
      }
    } catch (e) {
      statusEl.textContent = 'Could not load dividend picks. Check your connection and try again.';
    }
  }

  function renderDashboardPreview(data) {
    const wrap = document.getElementById('dashboard-signals-preview');
    wrap.innerHTML = '';

    const styleMeta = {
      'Intraday (watch)': { label: 'Intraday', duration: 'Today' },
      'Swing': { label: 'Short Term', duration: '1–3 weeks' },
      'Positional': { label: 'Long Term', duration: '2–6 months' },
    };
    const styleOrder = ['Intraday (watch)', 'Swing', 'Positional'];

    const bySt = {};
    (data.signals || []).forEach(s => { (bySt[s.style] = bySt[s.style] || []).push(s); });

    const grid = document.createElement('div');
    grid.className = 'preview-grid';

    styleOrder.forEach(style => {
      const meta = styleMeta[style];
      const list = (bySt[style] || []).filter(s => s.direction === 'Bullish');

      const card = document.createElement('div');
      card.className = 'card preview-card';
      card.innerHTML = `<div class="card-header"><h2>${meta.label} <span class="style-duration">(${meta.duration})</span></h2></div>`;

      if (list.length === 0) {
        card.innerHTML += '<div class="preview-empty">No bullish setup right now.</div>';
      } else {
        const rowsWrap = document.createElement('div');
        rowsWrap.className = 'rows';
        list.slice(0, 2).forEach(s => rowsWrap.appendChild(buildRow(s)));
        card.appendChild(rowsWrap);
      }

      const link = document.createElement('button');
      link.className = 'view-all-link';
      link.textContent = `View all ${list.length ? list.length + ' ' : ''}${meta.label} signals →`;
      link.addEventListener('click', () => switchTab('signals'));
      card.appendChild(link);

      grid.appendChild(card);
    });

    wrap.appendChild(grid);
  }

  function renderReport(data) {
    currentReportData = data;
    const container = document.getElementById('report-content');
    document.getElementById('last-generated').textContent = 'Last generated: ' + data.generated_at_display;
    document.getElementById('stat-scanned').textContent = data.stocks_scanned ?? '--';
    document.getElementById('stat-signals').textContent = (data.signals || []).length;
    container.innerHTML = '';

    renderMySelections();
    renderDashboardPreview(data);
    renderWatchlist();
    setTimeout(refreshSignalLivePrices, 0);

    if (data.ipos && data.ipos.length > 0) {
      const section = document.createElement('section');
      section.className = 'group';
      section.innerHTML = `<h2>IPO watch <span class="count">(${data.ipos.length})</span></h2>`;
      const rowsWrap = document.createElement('div');
      rowsWrap.className = 'rows';
      data.ipos.forEach(ipo => {
        const row = document.createElement('div');
        row.className = 'row ' + verdictClass(ipo.verdict);
        row.innerHTML = `
          <div class="name">${ipo.name} <span class="badge">${ipo.segment}</span></div>
          <div class="figures">
            <div><span class="flabel">Issue price</span>${ipo.issue_price}</div>
            <div><span class="flabel">GMP</span>${ipo.gmp}</div>
            <div><span class="flabel">Est. listing</span>${ipo.est_listing} (${ipo.gain_pct}%)</div>
          </div>
          <div class="reason">${ipo.verdict}${ipo.close_date ? ' · Closes ' + ipo.close_date : ''}</div>
        `;
        rowsWrap.appendChild(row);
      });
      section.appendChild(rowsWrap);
      container.appendChild(section);
    }

    // Short Term shown first ("mainly short term" priority), then Intraday, then Long Term.
    const styleMeta = {
      'Swing': { label: 'Short Term', duration: '1–3 weeks' },
      'Intraday (watch)': { label: 'Intraday', duration: 'Today' },
      'Positional': { label: 'Long Term', duration: '2–6 months' },
    };
    const styles = ['Swing', 'Intraday (watch)', 'Positional'];
    const bySt = {};
    (data.signals || []).forEach(s => {
      (bySt[s.style] = bySt[s.style] || []).push(s);
    });

    let anySignals = false;
    styles.forEach(style => {
      const list = bySt[style];
      if (!list || list.length === 0) return;
      anySignals = true;

      const bullish = list.filter(s => s.direction === 'Bullish');
      const other = list.filter(s => s.direction !== 'Bullish'); // bearish + unknown

      const meta = styleMeta[style] || { label: style, duration: '' };
      const section = document.createElement('section');
      section.className = 'group';
      section.innerHTML = `<h2>${meta.label} <span class="style-duration">(${meta.duration})</span> <span class="count">- ${list.length}</span></h2>`;

      if (bullish.length > 0) {
        const rowsWrap = document.createElement('div');
        rowsWrap.className = 'rows';
        bullish.forEach(s => rowsWrap.appendChild(buildRow(s)));
        section.appendChild(rowsWrap);
      }

      if (other.length > 0) {
        const details = document.createElement('details');
        details.className = 'caution-group';
        const summary = document.createElement('summary');
        summary.textContent = `${other.length} caution / avoid setup${other.length > 1 ? 's' : ''} - tap to view`;
        details.appendChild(summary);
        const rowsWrap = document.createElement('div');
        rowsWrap.className = 'rows';
        rowsWrap.style.marginTop = '10px';
        other.forEach(s => rowsWrap.appendChild(buildRow(s)));
        details.appendChild(rowsWrap);
        section.appendChild(details);
      }

      container.appendChild(section);
    });

    if (!anySignals && (!data.ipos || data.ipos.length === 0)) {
      container.innerHTML = "<div class=\"empty\">No stocks matched today's criteria. No forced trades - that's fine.</div>";
    }
  }

  async function loadCached() {
    const res = await fetch('/api/report');
    const json = await res.json();
    if (json.status === 'ok') {
      renderReport(json.data);
      if (json.stale) autoRefreshReport();
    } else {
      document.getElementById('report-content').innerHTML =
        "<div class=\"empty\">Running today's analysis automatically - scanning every main-board stock takes several minutes the first time...</div>";
      autoRefreshReport();
    }
  }

  // Runs on its own once per day (not on every visit) - the underlying
  // Entry/Stoploss/Target levels only change when a new trading day's
  // close is available, so regenerating more often than that just
  // recomputes the same numbers while hammering yfinance/NSE for nothing.
  // Live prices stay fresh independently via /api/live-price every 60s.
  async function autoRefreshReport() {
    const statusEl = document.getElementById('auto-refresh-status');
    try {
      const res = await fetch('/api/generate', { method: 'POST' });
      const json = await res.json();
      if (json.status === 'ok') {
        renderReport(json.data);
        if (statusEl) statusEl.style.display = 'none';
      } else {
        if (statusEl) { statusEl.style.display = 'block'; statusEl.textContent = "Starting the market scan in the background…"; }
        pollScan();
      }
    } catch (e) {
      if (statusEl) statusEl.textContent = "Couldn't auto-update right now - showing the last available report.";
    }
  }

  // Scanning every main-board stock takes several minutes, so the server runs it
  // in the background and we poll its progress here until the new report is ready.
  let pollTimer = null;
  function schedulePoll(onDone, ms) {
    if (pollTimer) clearTimeout(pollTimer);      // never run two polling loops at once
    pollTimer = setTimeout(() => pollScan(onDone), ms);
  }
  async function pollScan(onDone) {
    const statusEl = document.getElementById('auto-refresh-status');
    try {
      const res = await fetch('/api/scan-status');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const st = await res.json();
      scanRunning = !!st.running;
      if (st.running) {
        if (statusEl) {
          statusEl.style.display = 'block';
          statusEl.textContent = st.total
            ? `Scanning the market: ${st.done} of ${st.total} stocks checked…`
            : `${st.phase || 'Starting scan'}…`;
        }
        schedulePoll(onDone, 15000);
        return;
      }
      const r = await fetch('/api/report');
      const j = await r.json();
      if (j.status === 'ok') renderReport(j.data);
      if (statusEl) {
        if (st.error) { statusEl.style.display = 'block'; statusEl.textContent = "Scan failed: " + st.error; }
        else statusEl.style.display = 'none';
      }
    } catch (e) {
      if (statusEl) statusEl.textContent = "Server is busy scanning - retrying in 30 seconds…";
      schedulePoll(onDone, 30000);
      return;
    }
    if (onDone) onDone();
  }

  async function generateReport() {
    const btn = document.getElementById('generate-btn');
    const statusEl = document.getElementById('auto-refresh-status');
    btn.disabled = true;
    btn.textContent = 'Scanning...';
    if (statusEl) { statusEl.style.display = 'none'; }
    const finish = () => {
      btn.disabled = false; btn.textContent = 'Refresh now';
      lastRefreshedAt = Date.now(); nextRefreshAt = lastRefreshedAt + REFRESH_INTERVAL_MS;
    };
    try {
      await fetch('/api/generate?force=1', { method: 'POST' });
      pollScan(finish);
    } catch (e) {
      alert('Something went wrong refreshing the report. Try again.');
      finish();
    }
  }

  function downloadPdf() {
    switchTab('signals');
    // Expand any collapsed "caution" sections so their content isn't
    // silently missing from the printed/saved PDF.
    const detailsEls = document.querySelectorAll('details');
    const wasOpen = Array.from(detailsEls).map(d => d.open);
    detailsEls.forEach(d => d.open = true);

    window.print();

    setTimeout(() => {
      detailsEls.forEach((d, i) => d.open = wasOpen[i]);
    }, 1000);
  }

  // --- In-tab live price alerts (only fire while this tab is open) ---
  let notifyEnabled = false;
  let notifyTimer = null;
  const alreadyNotified = new Set(); // symbol|style keys we've already alerted for, this session

  function toggleNotifications() {
    const btn = document.getElementById('notify-btn');
    if (!('Notification' in window)) {
      alert('Your browser does not support notifications.');
      return;
    }
    if (!notifyEnabled) {
      Notification.requestPermission().then(perm => {
        if (perm === 'granted') {
          notifyEnabled = true;
          btn.classList.add('active');
          btn.textContent = '🔔 Alerts on';
          startNotifyLoop();
        } else {
          alert('Notification permission was not granted.');
        }
      });
    } else {
      notifyEnabled = false;
      btn.classList.remove('active');
      btn.textContent = '🔔 Enable alerts';
      if (notifyTimer) clearInterval(notifyTimer);
    }
  }

  function startNotifyLoop() {
    checkLivePrices(); // run once immediately
    if (notifyTimer) clearInterval(notifyTimer);
    notifyTimer = setInterval(checkLivePrices, 60000); // every 60 seconds
  }

  async function checkLivePrices() {
    if (!notifyEnabled || !currentReportData) return;
    const selKeys = getSelections();
    const tracked = (currentReportData.signals || []).filter(s => selKeys.includes(selectionKey(s)));
    if (tracked.length === 0) return;

    const symbols = [...new Set(tracked.map(s => s.symbol))];
    try {
      const res = await fetch('/api/live-price?symbols=' + encodeURIComponent(symbols.join(',')));
      const json = await res.json();
      if (json.status !== 'ok') return;
      const prices = json.prices || {};

      tracked.forEach(s => {
        const live = prices[s.symbol];
        if (live === undefined) return;
        const key = selectionKey(s);
        // "At entry" = live price within 0.3% of the entry level.
        const tolerance = s.entry * 0.003;
        const atEntry = Math.abs(live - s.entry) <= tolerance;
        if (atEntry && !alreadyNotified.has(key)) {
          alreadyNotified.add(key);
          const src = (json.sources || {})[s.symbol];
          const srcLabel = src === 'kite' ? 'real-time via Zerodha' : 'delayed, Yahoo Finance';
          new Notification(`${s.symbol_short} is at Entry point`, {
            body: `Live: ₹${live} (${srcLabel})  |  Entry: ₹${s.entry}  |  SL: ₹${s.stoploss}  |  Target: ₹${s.target}`,
          });
        }
      });
    } catch (e) {
      console.error('Live price check failed', e);
    }
  }

  // --- Stock search (any NSE/BSE symbol, not just the watchlist) ---
  document.getElementById('stock-search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchStock();
  });

  function trendClass(trend) {
    if (!trend) return 'other';
    if (trend.startsWith('Uptrend')) return 'Uptrend';
    if (trend.startsWith('Downtrend')) return 'Downtrend';
    return 'Mixed';
  }

  const TF_META = {
    'Intraday (watch)': { label: 'Intraday', duration: 'Today' },
    'Swing': { label: 'Short Term', duration: '1–3 weeks' },
    'Positional': { label: 'Long Term', duration: '2–6 months' },
  };

  let lastSearchData = null;

  function buildTfRow(tf) {
    const meta = TF_META[tf.style] || { label: tf.style, duration: '' };
    const div = document.createElement('div');
    div.className = 'tf-row' + (tf.has_setup ? ' has-setup' : '');
    if (tf.has_setup) {
      const warningHtml = tf.warning ? `<div class="caution-banner">⚠ ${tf.warning}</div>` : '';
      div.innerHTML = `
        <div class="tf-title">${meta.label} <span class="tf-duration">(${meta.duration})</span></div>
        <div class="figures">
          <div><span class="flabel">📍 Entry</span>${tf.entry}</div>
          <div><span class="flabel">🛑 Stoploss</span>${tf.stoploss}</div>
          <div><span class="flabel">🎯 Target</span>${tf.target}</div>
        </div>
        <div class="live-price-row">R:R ~1:${tf.risk_reward}</div>
        ${warningHtml}
        <div class="reason">${tf.reason}</div>
      `;
    } else {
      div.innerHTML = `
        <div class="tf-title">${meta.label} <span class="tf-duration">(${meta.duration})</span></div>
        <div class="no-setup-tag">No entry setup right now</div>
        <div class="reason" style="margin-top:6px">${tf.reason}</div>
      `;
    }
    return div;
  }

  function renderCandidateList(data) {
    const wrap = document.getElementById('search-result');
    const list = document.createElement('div');
    list.className = 'search-card';
    list.innerHTML = `<div>Multiple matches for "${data.query}" - which one?</div>`;
    const candWrap = document.createElement('div');
    candWrap.className = 'candidate-list';
    data.candidates.forEach(c => {
      const btn = document.createElement('button');
      btn.className = 'secondary';
      btn.textContent = `${c.name} (${c.symbol.replace('.NS', '').replace('.BO', '')})`;
      btn.addEventListener('click', () => runSearch(c.symbol.replace('.NS', '').replace('.BO', '')));
      candWrap.appendChild(btn);
    });
    list.appendChild(candWrap);
    wrap.innerHTML = '';
    wrap.appendChild(list);
  }

  function renderSearchResult(data) {
    lastSearchData = data;
    const wrap = document.getElementById('search-result');
    wrap.innerHTML = '';

    const card = document.createElement('div');
    card.className = 'search-card';

    const s = data.snapshot;
    const price = data.live_price !== null && data.live_price !== undefined ? data.live_price : s.close;
    const changeColor = s.day_change_pct >= 0 ? 'var(--green)' : 'var(--red)';
    const changeSign = s.day_change_pct >= 0 ? '+' : '';
    const liveTag = data.live_price !== null && data.live_price !== undefined
      ? (data.live_price_source === 'kite' ? 'Real-time via Zerodha Kite Connect' : 'Delayed (up to ~15 min) via Yahoo Finance')
      : 'Live price unavailable - showing last close';

    const trackableTfs = (data.timeframes || []).filter(tf => tf.has_setup);
    const anyTracked = trackableTfs.some(tf => isSelected({ symbol: tf.symbol, style: tf.style }));
    const starLabel = trackableTfs.length === 0
      ? ''
      : `<div style="margin-top:6px"><button class="star-btn ${anyTracked ? 'selected' : ''}" id="stock-star-btn">${anyTracked ? '★ In My Suggestions' : '☆ Add to My Suggestions'}</button></div>`;

    card.innerHTML = `
      <div class="sc-header">
        <div><span class="sc-name">${data.name}</span><span class="sc-symbol">${data.symbol_short} · ${data.exchange}</span></div>
        <div style="text-align:right">
          <span class="sc-price">₹${price}</span>${sourceBadge(data.live_price_source)}
          <span class="sc-change" style="color:${changeColor}">${changeSign}${s.day_change_pct}%</span>
          <span class="sc-livetag">${liveTag}</span>
          ${starLabel}
        </div>
      </div>
      <div class="snapshot-grid">
        <div><span class="flabel">Trend</span><span class="trend-badge ${trendClass(s.trend)}">${s.trend}</span></div>
        <div><span class="flabel">RSI (14)</span>${s.rsi ?? '—'}</div>
        <div><span class="flabel">52-wk range</span>₹${s.week_range_low} – ₹${s.week_range_high}</div>
        <div><span class="flabel">Volume vs 20d avg</span>${s.volume_vs_20d_avg ? s.volume_vs_20d_avg + '×' : '—'}</div>
      </div>
    `;
    const starBtn = card.querySelector('#stock-star-btn');
    if (starBtn) starBtn.addEventListener('click', () => toggleStockTrack(data));

    if (data.warnings && data.warnings.length > 0) {
      data.warnings.forEach(w => {
        const banner = document.createElement('div');
        banner.className = 'caution-banner';
        banner.textContent = '⚠ ' + w;
        card.appendChild(banner);
      });
    }

    const tfWrap = document.createElement('div');
    tfWrap.className = 'tf-rows';
    tfWrap.style.marginTop = '14px';
    (data.timeframes || []).forEach(tf => tfWrap.appendChild(buildTfRow(tf)));
    card.appendChild(tfWrap);

    if (data.news && data.news.length > 0) {
      const newsDiv = document.createElement('div');
      newsDiv.className = 'news';
      newsDiv.style.marginTop = '14px';
      newsDiv.innerHTML = data.news.map(n => `<a href="${n.link}" target="_blank">${n.title}</a>`).join('<br>');
      card.appendChild(newsDiv);
    }

    const note = document.createElement('div');
    note.className = 'data-note';
    note.textContent = data.data_note || '';
    card.appendChild(note);

    wrap.appendChild(card);
  }

  async function runSearch(q) {
    const wrap = document.getElementById('search-result');
    wrap.innerHTML = '<div class="empty">Searching...</div>';
    try {
      const res = await fetch('/api/search?q=' + encodeURIComponent(q));
      const json = await res.json();
      if (json.status !== 'ok') {
        wrap.innerHTML = `<div class="empty">${json.message || 'Something went wrong searching for that stock.'}</div>`;
        return;
      }
      if (json.data.status === 'multiple_matches') {
        renderCandidateList(json.data);
        return;
      }
      renderSearchResult(json.data);
    } catch (e) {
      wrap.innerHTML = '<div class="empty">Something went wrong searching for that stock. Try again.</div>';
    }
  }

  function searchStock() {
    const q = document.getElementById('stock-search-input').value.trim();
    if (!q) return;
    runSearch(q);
  }

  loadCached();
  trackRefresh('News', () => loadNewsDigest());
  trackRefresh('Results', () => loadResultsCalendar());
  trackRefresh('Stocks', () => renderWatchlist());
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # threaded=True is important now that a report can auto-generate in the
    # background (see /api/generate's once-a-day auto-refresh): without it,
    # Flask's dev server handles one request at a time, so a 1-2 minute
    # generation would block every other request - live prices, other tabs,
    # everything - until it finished.
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
