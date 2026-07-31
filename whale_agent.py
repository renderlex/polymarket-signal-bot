"""
Polymarket trading engine.
Single strategy only: Signal Scalp (I), recalibrated for 4-24h cycles.
"""
import os
import time
import json
import math
import logging
import threading
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

FEED = None
NOTIFY = None
_STOP_NOW = False
_SAVE_CB = None

BINANCE = "https://api.binance.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

COINS = {
    "BTC": ("BTCUSDT", "btc"),
    "ETH": ("ETHUSDT", "eth"),
    "SOL": ("SOLUSDT", "sol"),
    "XRP": ("XRPUSDT", "xrp"),
    "DOG": ("DOGEUSDT", "doge"),
    "BNB": ("BNBUSDT", "bnb"),
}

HISTORY_DIR = os.getenv("HISTORY_DIR", "chart_history")
TRADE_LOG = os.getenv("TRADE_LOG", "trade_history.json")

WINDOW = int(os.getenv("WINDOW", "14400"))
SUFFIX = {300: "5m", 900: "15m", 14400: "4h", 86400: "1d"}.get(WINDOW, "4h")

STAKE = float(os.getenv("STAKE", "1"))
DEMO_START = float(os.getenv("DEMO_START_BALANCE", "100"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "20"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "15"))

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"

CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
MAX_REAL_STAKE = float(os.getenv("MAX_REAL_STAKE", "25"))
MAX_WINDOW_EXPOSURE = float(os.getenv("MAX_WINDOW_EXPOSURE", "25"))
MAX_BUY = float(os.getenv("MAX_BUY", "0.95"))
REAL_CLIENT = None

SIG_MA_FAST = int(os.getenv("SIG_MA_FAST", "12"))
SIG_MA_SLOW = int(os.getenv("SIG_MA_SLOW", "48"))
SIG_RSI_PERIOD = int(os.getenv("SIG_RSI_PERIOD", "14"))
# Strict RSI trend filtering -- no neutral-zone (chop/whipsaw) entries.
SIG_RSI_NEUTRAL_MIN = float(os.getenv("SIG_RSI_NEUTRAL_MIN", "45"))   # hard veto band: never enter inside [MIN, MAX]
SIG_RSI_NEUTRAL_MAX = float(os.getenv("SIG_RSI_NEUTRAL_MAX", "57"))
# Oversold/overbought default to the neutral-zone edges themselves (no extra
# "dead zone" 40-45 / 57-60 that was blocking entries beyond what was asked).
# SIG_RSI_OVERSOLD/OVERBOUGHT/CROSS_* constants removed 2026-07-29: they used
# to require RSI to specifically be on the "dip" side to confirm an entry
# (see hourly_signal below for why that was overly strict).
SIG_MIN_MA_GAP = float(os.getenv("SIG_MIN_MA_GAP", "0.001"))
SIG_PRICE_MIN = float(os.getenv("SIG_PRICE_MIN", "0.10"))
SIG_PRICE_MAX = float(os.getenv("SIG_PRICE_MAX", "0.90"))
SIG_SPREAD_MAX = float(os.getenv("SIG_SPREAD_MAX", "0.16"))
SIG_TP = float(os.getenv("SIG_TP", "0.30"))
SIG_SL = float(os.getenv("SIG_SL", "0.22"))
SIG_TRAIL_PCT = float(os.getenv("SIG_TRAIL_PCT", "0.16"))
SIG_MIN_HOLD_BEFORE_TRAIL = float(os.getenv("SIG_MIN_HOLD_BEFORE_TRAIL", "1"))
SIG_MIN_HOLD_BEFORE_REVERSAL = float(os.getenv("SIG_MIN_HOLD_BEFORE_REVERSAL", "0.5"))
SIG_HOPELESS_PRICE = float(os.getenv("SIG_HOPELESS_PRICE", "0.20"))
SIG_HOPELESS_MIN_LEFT = int(os.getenv("SIG_HOPELESS_MIN_LEFT", "30"))
SIG_LOCK_ARM_PCT = float(os.getenv("SIG_LOCK_ARM_PCT", "0.06"))
SIG_LOCK_GIVE = float(os.getenv("SIG_LOCK_GIVE", "0.65"))
SIG_MAX_HOLD_H = float(os.getenv("SIG_MAX_HOLD_H", "36"))
# How often (seconds) the bot re-checks entry/exit signals. Used to default
# to 600 (10 min), which meant the bot only ever *looked* at the market 6
# times an hour -- any profitable move that appeared and reversed between
# two checks was simply never seen. 60s means it checks roughly as often as
# the live chart itself refreshes.
SIG_POLL = int(os.getenv("SIG_POLL", "5"))  # seconds between decision cycles

# No new entries (and no scale-ins) within the last N minutes of the window.
# Positions already open are still allowed to exit until the very end; this
# only blocks opening fresh exposure right before the market resolves.
SIG_ENTRY_CUTOFF_MIN = int(os.getenv("SIG_ENTRY_CUTOFF_MIN", "30"))

SIG_MAX_POS = int(os.getenv("SIG_MAX_POS", "3"))
SIG_REENTRY_CD = int(os.getenv("SIG_REENTRY_CD", "1800"))

# Phased position sizing: split a position into several packets instead of
# one atomic all-in / all-out order.
SIG_ENTRY_TRANCHES = max(1, int(os.getenv("SIG_ENTRY_TRANCHES", "1")))  # one entry per position (no scale-in)
SIG_SCALE_MAX_ADVERSE = float(os.getenv("SIG_SCALE_MAX_ADVERSE", "0.05"))  # skip next tranche if price moved >5% against last fill
SIG_PARTIAL_TP_PCT = float(os.getenv("SIG_PARTIAL_TP_PCT", "0.08"))  # quick-profit trigger to trim a winning packet
SIG_PARTIAL_SL_PCT = float(os.getenv("SIG_PARTIAL_SL_PCT", "0.10"))  # minor-drawdown trigger to trim a losing packet

# Dynamic entry price cap based on how far into the window we are.
# Early in the window, prices near 0.90 are risky (uncertain outcome);
# late in the window, a high price means the market has already decided
# and a near-1.0 outcome is likely, so we allow trading closer to 1.0.
def _entry_price_cap(now, ws):
    progress = (now - ws) / WINDOW
    if progress < 0.50:
        return 0.70
    elif progress < 0.75:
        return 0.80
    else:
        return 0.95

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("whale_agent.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("whale")


def g(url, **params):
    return requests.get(url, params=params, timeout=12)


def pm_market(prefix, ws):
    d = g(f"{GAMMA}/events", slug=f"{prefix}-updown-{SUFFIX}-{ws}").json()
    if not d:
        return None
    m = d[0]["markets"][0]
    toks = m.get("clobTokenIds")
    if isinstance(toks, str):
        toks = json.loads(toks)
    op = m.get("outcomePrices")
    if isinstance(op, str):
        op = json.loads(op)
    return {
        "slug": f"{prefix}-updown-{SUFFIX}-{ws}",
        "toks": toks,
        "outcomePrices": op,
        "url": f"https://polymarket.com/event/{prefix}-updown-{SUFFIX}-{ws}",
    }


def _book_fast(token, timeout=4):
    try:
        return requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=timeout).json()
    except Exception:
        return None


_BOOK_CACHE = {}
_BOOK_CACHE_TTL = 5.0


def _book(token):
    now = time.time()
    cached = _BOOK_CACHE.get(token)
    if cached and now - cached[0] < _BOOK_CACHE_TTL:
        return cached[1]
    bk = _book_fast(token)
    _BOOK_CACHE[token] = (time.time(), bk)
    return bk


def pm_book(token):
    bk = _book(token)
    if not bk:
        return None
    asks = bk.get("asks", [])
    bids = bk.get("bids", [])
    if not asks or not bids:
        return None
    ask = min(float(a["price"]) for a in asks)
    bid = max(float(b["price"]) for b in bids)
    return {"ask": ask, "bid": bid, "mid": round((ask + bid) / 2, 4), "spread": round(ask - bid, 4)}


def pm_realistic_fill(token, stake):
    bk = _book(token)
    if not bk:
        return None
    asks = sorted(bk.get("asks", []), key=lambda a: float(a["price"]))
    bids = sorted(bk.get("bids", []), key=lambda b: float(b["price"]), reverse=True)
    if not asks:
        return None
    best_ask = float(asks[0]["price"])
    best_bid = float(bids[0]["price"]) if bids else 0.0
    spread = round(best_ask - best_bid, 4)

    spent = 0.0
    shares = 0.0
    for level in asks:
        price = float(level["price"])
        size = float(level["size"])
        if price > MAX_BUY:
            break
        if price < SIG_PRICE_MIN or price > SIG_PRICE_MAX:
            continue
        cost = price * size
        if spent + cost >= stake:
            take = (stake - spent) / price
            shares += take
            spent = stake
            break
        spent += cost
        shares += size

    if shares <= 0:
        return None
    return {
        "vwap": round(spent / shares, 4),
        "shares": round(shares, 4),
        "spent": round(spent, 4),
        "filled_pct": round(spent / stake * 100, 1),
        "spread": spread,
    }


def pm_realistic_sell(token, shares):
    bk = _book(token)
    if not bk:
        return None
    bids = sorted(bk.get("bids", []), key=lambda b: float(b["price"]), reverse=True)
    if not bids:
        return None

    proceeds = 0.0
    sold = 0.0
    need = shares
    for level in bids:
        price = float(level["price"])
        size = float(level["size"])
        take = min(size, need)
        proceeds += price * take
        sold += take
        need -= take
        if need <= 1e-9:
            break

    if sold <= 0:
        return None
    return {
        "vwap": round(proceeds / sold, 4),
        "proceeds": round(proceeds, 4),
        "sold": round(sold, 4),
        "filled_pct": round(sold / shares * 100, 1),
    }


def pm_resolution(prefix, ws):
    mk = pm_market(prefix, ws)
    if not mk or not mk.get("outcomePrices"):
        return None
    op = mk["outcomePrices"]
    return "UP" if float(op[0]) >= 0.99 else ("DOWN" if float(op[1]) >= 0.99 else None)


def get_real_client():
    global REAL_CLIENT
    if REAL_CLIENT is not None:
        return REAL_CLIENT
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = os.environ.get("PRIVATE_KEY")
    if not pk:
        raise RuntimeError("PRIVATE_KEY is missing in .env")
    funder = os.getenv("FUNDER_ADDRESS") or None
    c = ClobClient(CLOB, chain_id=CHAIN_ID, key=pk, signature_type=SIGNATURE_TYPE, funder=funder)

    ek, es, ep = os.getenv("POLY_API_KEY"), os.getenv("POLY_API_SECRET"), os.getenv("POLY_API_PASSPHRASE")
    if ek and es and ep:
        c.set_api_creds(ApiCreds(api_key=ek, api_secret=es, api_passphrase=ep))
        try:
            c.get_api_keys()
        except Exception:
            creds = c.create_or_derive_api_creds()
            c.set_api_creds(creds)
    else:
        creds = c.create_or_derive_api_creds()
        c.set_api_creds(creds)

    REAL_CLIENT = c
    return c


def usdc_balance():
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    c = get_real_client()
    ba = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return int(ba.get("balance", "0")) / 1e6


def _ceil_tick(price, tick):
    return min(MAX_BUY, round(math.ceil(price / tick) * tick, 4))


def _parse_fill(resp):
    if not isinstance(resp, dict):
        return None
    mk = resp.get("makingAmount")
    tk = resp.get("takingAmount")
    try:
        spent = float(mk) if mk is not None else None
        shares = float(tk) if tk is not None else None
        if spent and shares and spent > 0 and shares > 0:
            return {"shares": round(shares, 4), "spent": round(spent, 4)}
    except Exception:
        pass
    return None


def execute(token, side, shares, price, demo, amount=None):
    if demo:
        return {"status": "demo", "fill": price}
    if DRY_RUN or not LIVE_TRADING:
        log.info("[DRY-RUN] order skipped (%s %s %.2f @ %.3f)", side, token[:10], shares, price)
        return {"status": "dry_run"}

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    try:
        amt = round(min(float(amount if amount is not None else shares * price), MAX_REAL_STAKE), 2)
        if amt <= 0:
            return {"status": "error", "error": "zero amount"}
        c = get_real_client()
        tick = float(c.get_tick_size(token))
        cap = _ceil_tick(price, tick)
        args = MarketOrderArgs(token_id=token, amount=amt, side=BUY, price=cap, order_type=OrderType.FAK)
        signed = c.create_market_order(args)
        resp = c.post_order(signed, OrderType.FAK)
        ok = isinstance(resp, dict) and (resp.get("success") or resp.get("status") in ("matched", "live"))
        oid = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
        if not ok:
            return {"status": "rejected", "resp": resp}
        return {"status": "live", "order_id": oid, "resp": resp, "filled": _parse_fill(resp), "cap": cap}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _close_b(pos, sell, reason, bal, bets, fee_rate=0.001):
    pos["sell"] = sell["vwap"]
    pos["proceeds"] = sell["proceeds"]
    fee = round((sell["proceeds"] + pos["spent"]) * fee_rate, 4)
    pos["fee"] = fee
    pos["pnl"] = round(sell["proceeds"] - pos["spent"] - fee, 4)
    bal = round(bal + pos["pnl"], 2)
    pos["result"] = "WIN" if pos["pnl"] >= 0 else "LOSS"
    pos["balance"] = bal
    pos["exit_reason"] = reason
    pos["real_outcome"] = "EXIT"
    log.info(
        "EXIT %s %s buy@%.3f -> sell@%.3f (%s) | PnL %+.2f | bal %.2f",
        pos["coin"],
        pos["side"],
        pos["buy"],
        sell["vwap"],
        reason,
        pos["pnl"],
        bal,
    )
    bets.append(pos)
    if NOTIFY:
        try:
            NOTIFY("b_exit", pos)
        except Exception:
            pass
    return bal


def _settle_one(pos, out, bal):
    won = pos["side"] == out
    pos["real_outcome"] = out
    pos["result"] = "WIN" if won else "LOSS"
    pos["pnl"] = round((pos["shares"] - pos["spent"]) if won else -pos["spent"], 4)
    bal = round(bal + pos["pnl"], 2)
    pos["balance"] = bal
    log.info("SETTLE %s %s %s | PnL %+.2f | balance %.2f", pos.get("coin","?"), pos.get("side","?"), pos["result"], pos["pnl"], bal)
    _append_trade_log({"kind": "settle", "coin": pos.get("coin","?"), "t": time.time(), "price": 0,
                        "side": pos.get("side","?"), "reason": pos["result"], "pnl": pos["pnl"]})
    if NOTIFY:
        try:
            NOTIFY("b_settle", pos)
        except Exception:
            pass
    return bal


def settle_pending(bets, bal):
    settled = []
    for pos in bets:
        if pos.get("result") != "PENDING" or pos.get("gave_up"):
            continue
        out = pm_resolution(pos["prefix"], pos["ws"])
        if out is None:
            pos["settle_tries"] = pos.get("settle_tries", 0) + 1
            if pos["settle_tries"] >= 120:
                pos["gave_up"] = True
                log.info("PENDING too long: %s %s", pos.get("coin"), pos.get("side"))
            continue
        bal = _settle_one(pos, out, bal)
        settled.append(pos)
    return bal, settled


def risk_stop(bal, peak, day0):
    dd = (peak - bal) / peak * 100 if peak > 0 else 0
    day = (day0 - bal) / day0 * 100 if day0 > 0 else 0
    if dd >= MAX_DRAWDOWN_PCT:
        log.error("STOP drawdown %.1f%%", dd)
        return True
    if day >= MAX_DAILY_LOSS_PCT:
        log.error("STOP daily loss %.1f%%", day)
        return True
    return False


def _fetch_hourly_closes(symbol, limit=180):
    try:
        data = g(f"{BINANCE}/api/v3/klines", symbol=symbol, interval="1h", limit=limit).json()
        if not isinstance(data, list) or len(data) < SIG_MA_SLOW + 2:
            return None
        return [float(c[4]) for c in data]
    except Exception:
        return None


def _snap_price(symbol):
    """Binance ticker for chart snapshots — short timeout, skip on delay."""
    try:
        d = requests.get(f"{BINANCE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=3).json()
        return float(d["price"])
    except Exception:
        return None


# ── Live chart recording: Binance price vs Polymarket price + entry/exit markers ──
CHART_MAXLEN = 400
CHART_EVENTS_MAXLEN = 100
CHART_DATA = {}  # coin -> {"t": [...], "binance": [...], "pm_bid": [...], "pm_ask": [...], "events": [...]}


def _chart_slot(coin):
    return CHART_DATA.setdefault(coin, {"t": [], "binance": [], "pm_bid": [], "pm_ask": [], "events": []})


def _chart_snapshot(coin, t, binance_px, bid, ask):
    if binance_px is None or bid is None:
        return
    d = _chart_slot(coin)
    d["t"].append(t)
    d["binance"].append(binance_px)
    d["pm_bid"].append(bid)
    d["pm_ask"].append(ask if ask is not None else bid)
    if len(d["t"]) > CHART_MAXLEN:
        for key in ("t", "binance", "pm_bid", "pm_ask"):
            d[key] = d[key][-CHART_MAXLEN:]


def _chart_event(coin, t, kind, price, side, reason="", pnl=None):
    d = _chart_slot(coin)
    ev = {"coin": coin, "t": t, "kind": kind, "price": price, "side": side, "reason": reason, "pnl": pnl}
    d["events"].append(ev)
    _append_trade_log(ev)
    if len(d["events"]) > CHART_EVENTS_MAXLEN:
        d["events"] = d["events"][-CHART_EVENTS_MAXLEN:]


_SNAP_THREAD = None
_SNAP_LOCK = threading.Lock()
_SNAP_IN_FLIGHT = threading.Event()
_SNAP_ENABLED = True
_SNAP_INTERVAL = 60.0


def _snapshot_worker(markets):
    """Background thread: record chart snapshots every _SNAP_INTERVAL seconds
    so Binance/Polymarket book calls never block trading decisions."""
    global _SNAP_ENABLED
    while _SNAP_ENABLED:
        _SNAP_IN_FLIGHT.set()
        try:
            _record_chart_snapshot(markets, time.time())
            _persist_chart_data()
        except Exception:
            pass
        finally:
            _SNAP_IN_FLIGHT.clear()
        for _ in range(int(_SNAP_INTERVAL)):
            time.sleep(1)
            if not _SNAP_ENABLED:
                break


def _snapshot_start(markets):
    """Launch (once) the background chart-snapshot thread for a window."""
    global _SNAP_THREAD
    if _SNAP_THREAD is not None and _SNAP_THREAD.is_alive():
        return
    _SNAP_THREAD = threading.Thread(target=_snapshot_worker, args=(markets,), daemon=True)
    _SNAP_THREAD.start()


def _snapshot_stop():
    global _SNAP_ENABLED
    _SNAP_ENABLED = False


def _record_chart_snapshot(markets, t):
    """Record one Binance-vs-Polymarket price point per coin for the live
    chart. Short timeouts so it never stalls the trading loop."""
    for coin, mk in markets.items():
        try:
            px = _snap_price(COINS[coin][0])
            bk = pm_book(mk["toks"][0])
            if px is not None and bk:
                _chart_snapshot(coin, t, px, bk["bid"], bk["ask"])
        except Exception:
            pass


CHART_FILE = os.getenv("CHART_FILE", "chart_data.json")


def _persist_chart_data():
    """Dump CHART_DATA to disk so an independent viewer process (chart_gui.py)
    can render it without touching the running bot's memory. Atomic write via
    temp file + replace so the reader never sees a half-written file."""
    try:
        tmp = CHART_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CHART_DATA, f)
        os.replace(tmp, CHART_FILE)
    except Exception:
        pass


def generate_chart_png(coin):
    """Render a PNG chart of Binance price vs Polymarket UP price for `coin`,
    with entry/scale-in/exit markers from CHART_DATA. Returns PNG bytes, or
    None if there isn't enough recorded data yet or matplotlib is unavailable."""
    d = CHART_DATA.get(coin)
    if not d or len(d["t"]) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io
        from datetime import datetime as _dt
    except Exception:
        return None

    times = [_dt.fromtimestamp(t) for t in d["t"]]
    fig, ax1 = plt.subplots(figsize=(9, 5), dpi=110)
    ax1.plot(times, d["pm_bid"], color="#4dabf7", label="Polymarket UP (bid)", linewidth=1.8)
    ax1.set_ylabel("Polymarket price (0-1)")
    ax1.set_ylim(0, 1)

    ax2 = ax1.twinx()
    ax2.plot(times, d["binance"], color="#ffa94d", label="Binance", linewidth=1.8)
    ax2.set_ylabel(f"{coin} price ($)")

    for ev in d["events"]:
        et = _dt.fromtimestamp(ev["t"])
        fill_price = ev["price"]
        if fill_price is None:
            continue
        # Snap marker Y to the nearest pm_bid snapshot so the marker sits on the curve
        nearest_idx = min(range(len(d["t"])), key=lambda i: abs(d["t"][i] - ev["t"]))
        marker_y = d["pm_bid"][nearest_idx]
        ax1.axvline(x=et, color="#868e96", linestyle="--", linewidth=0.5, alpha=0.4, zorder=2)
        ax1.scatter([et], [marker_y], s=120, zorder=5, edgecolors="white", linewidth=0.8)
        if ev["kind"] in ("entry", "scale_in"):
            label = "ENTRY" if ev["kind"] == "entry" else "scale-in"
            ax1.scatter([et], [marker_y], color="#37b24d", marker="^", s=120, zorder=6, edgecolors="white", linewidth=0.8)
            ax1.annotate(f"\u25b2 {label} {ev['side']}@{fill_price:.2f}", (et, marker_y),
                         xytext=(10, 10), textcoords="offset points", fontsize=8, color="#2f9e44",
                         arrowprops=dict(arrowstyle="->", color="#2f9e44", lw=0.8))
        else:
            ax1.scatter([et], [marker_y], color="#e03131", marker="v", s=120, zorder=6, edgecolors="white", linewidth=0.8)
            pnl_str = ""
            if ev.get("pnl") is not None:
                sgn = "+" if ev["pnl"] >= 0 else ""
                pnl_str = f" {sgn}${ev['pnl']:.2f}"
            ax1.annotate(f"\u25bc {ev['side']} {ev['reason']}@{fill_price:.2f}{pnl_str}", (et, marker_y),
                         xytext=(10, -14), textcoords="offset points", fontsize=8, color="#c92a2a",
                         arrowprops=dict(arrowstyle="->", color="#c92a2a", lw=0.8))

    ax1.set_title(f"{coin} \u2014 Signal Scalp: Binance vs Polymarket UP")
    fig.autofmt_xdate()
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _ensure_history_dir():
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
    except Exception:
        pass


def _save_window_chart(coin, window_ts):
    _ensure_history_dir()
    try:
        png = generate_chart_png(coin)
        if png is None:
            return
        dt = datetime.fromtimestamp(window_ts, timezone.utc).strftime("%Y-%m-%d_%H-%M")
        path = os.path.join(HISTORY_DIR, f"{coin}_{dt}.png")
        with open(path, "wb") as f:
            f.write(png)
    except Exception:
        pass


def _append_trade_log(event):
    _ensure_history_dir()
    path = os.path.join(HISTORY_DIR, TRADE_LOG)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    txt = os.path.join(HISTORY_DIR, "trades.log")
    try:
        kind = event.get("kind", "?")
        coin = event.get("coin", event.get("side", "?"))
        side = event.get("side", "")
        price = event.get("price", 0)
        reason = event.get("reason", "")
        pnl = event.get("pnl")
        ts = datetime.fromtimestamp(event.get("t", 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        parts = [ts, kind.upper(), coin]
        if kind in ("entry", "scale_in"):
            parts.append(f"{side}@{price:.3f}")
        elif kind == "exit":
            parts.append(f"{side}@{price:.3f}")
            if pnl is not None:
                sgn = "+" if pnl >= 0 else ""
                parts.append(f"{sgn}${pnl:.2f}")
        if reason:
            parts.append(reason)
        with open(txt, "a", encoding="utf-8") as f:
            f.write(" | ".join(parts) + "\n")
    except Exception:
        pass


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    avg = sum(values[:period]) / period
    for v in values[period:]:
        avg = v * k + avg * (1 - k)
    return avg


def _rsi(values, period=14):
    """Return the Wilder RSI series (one value per closed candle after warm-up).
    The last element is the current RSI; the second-to-last is the prior
    candle's RSI, used to detect confirmed reversal crosses."""
    if len(values) < period + 2:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _to_rsi(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    series = [_to_rsi(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        series.append(_to_rsi(avg_gain, avg_loss))
    return series


_SIG_CACHE = {}
_SIG_CACHE_TTL = 30.0


def hourly_signal(symbol):
    now = time.time()
    cached = _SIG_CACHE.get(symbol)
    if cached and now - cached[0] < _SIG_CACHE_TTL:
        return cached[1]
    result = _hourly_signal_uncached(symbol)
    _SIG_CACHE[symbol] = (time.time(), result)
    return result


def _hourly_signal_uncached(symbol):
    closes = _fetch_hourly_closes(symbol)
    if not closes:
        return None
    fast = _ema(closes, SIG_MA_FAST)
    slow = _ema(closes, SIG_MA_SLOW)
    rsi_series = _rsi(closes, SIG_RSI_PERIOD)
    if fast is None or slow is None or not rsi_series:
        return None

    rsi = rsi_series[-1]
    gap = (fast - slow) / slow

    side = None
    # Trend direction from MA gap. RSI only blocks extreme exhaustion
    # (overbought > 80 when gap positive, oversold < 20 when gap negative).
    # No neutral-zone veto -- RSI 45-57 is normal chop, not a reason to skip.
    if gap >= SIG_MIN_MA_GAP:
        side = "UP"
    elif gap <= -SIG_MIN_MA_GAP:
        side = "DOWN"

    if not side:
        return None

    # Exhaustion guard: skip if RSI is extreme in the wrong direction
    if side == "UP" and rsi > 80:
        return None
    if side == "DOWN" and rsi < 20:
        return None

    strength = min(0.99, max(0.51, 0.5 + min(0.2, abs(gap) * 10) + abs(rsi - 50) / 500))
    return {
        "side": side,
        "fast": fast,
        "slow": slow,
        "rsi": rsi,
        "gap": gap,
        "strength": strength,
        "reason": f"1h MA{SIG_MA_FAST}/{SIG_MA_SLOW} gap {gap:+.3%}, RSI {rsi:.1f}",
    }


class PhasedPosition:
    """
    Manages one Signal Scalp position as a set of independently entered and
    independently exited packets (tranches), instead of one atomic
    all-in / all-out order.

    * Entry: capital is split into `max_tranches` equal packets. The first
      packet opens the position; later packets scale in on later polls only
      if the trend still agrees and price hasn't moved sharply against us.
    * Exit: full exit (TP/SL/max-hold/reversal) closes every open packet.
      Between those extremes, partial take-profit trims the single
      best-performing open packet on a quick profit spike, and partial
      stop-loss trims the single worst-performing open packet on a minor
      drawdown -- letting the rest of the position keep riding the
      4-24h macro trend instead of panic-closing everything.
    """

    _next_id = 1

    def __init__(self, coin, prefix, ws, side, idx, url, slug, ai_conf, ai_reason, target_stake, max_tranches):
        self.id = PhasedPosition._next_id
        PhasedPosition._next_id += 1
        self.coin = coin
        self.prefix = prefix
        self.ws = ws
        self.side = side
        self.idx = idx
        self.url = url
        self.slug = slug
        self.ai_conf = ai_conf
        self.ai_reason = ai_reason
        self.target_stake = target_stake
        self.max_tranches = max_tranches
        self.tranche_stake = round(target_stake / max_tranches, 4)
        self.entry_t = time.time()
        self.packets = []  # every packet ever opened (open + closed)
        self.partial_tp_stage = 0
        self.partial_sl_stage = 0
        self.trail_high = 0.0  # highest exit price seen (for trailing SL)

    def add_packet(self, fill, ex, now, live):
        pkt = {
            "pos_id": self.id,
            "tranche": len(self.packets) + 1,
            "coin": self.coin,
            "prefix": self.prefix,
            "ws": self.ws,
            "side": self.side,
            "idx": self.idx,
            "strategy": "I",
            "buy": fill["vwap"],
            "shares": fill["shares"],
            "spent": fill["spent"],
            "filled_pct": fill["filled_pct"],
            "entry_t": now,
            "url": self.url,
            "slug": self.slug,
            "live": live,
            "order_id": ex.get("order_id"),
            "ai_conf": self.ai_conf,
            "ai_reason": self.ai_reason,
            "status": "open",
        }
        self.packets.append(pkt)
        return pkt

    def open_packets(self):
        return [p for p in self.packets if p["status"] == "open"]

    def tranches_filled(self):
        return len(self.packets)

    def is_fully_deployed(self):
        return self.tranches_filled() >= self.max_tranches

    def total_shares(self):
        return sum(p["shares"] for p in self.open_packets())

    def total_spent(self):
        return sum(p["spent"] for p in self.open_packets())

    def last_buy(self):
        op = self.open_packets()
        if op:
            return op[-1]["buy"]
        return self.packets[-1]["buy"] if self.packets else 0.0

    def unrealized_pnl_pct(self, bid):
        spent = self.total_spent()
        if spent <= 0:
            return 0.0
        return (bid * self.total_shares() - spent) / spent

    def worst_open_packet(self, bid):
        op = self.open_packets()
        if not op:
            return None
        return min(op, key=lambda p: (bid - p["buy"]) / p["buy"])

    def best_open_packet(self, bid):
        op = self.open_packets()
        if not op:
            return None
        return max(op, key=lambda p: (bid - p["buy"]) / p["buy"])

    def is_flat(self):
        return not self.open_packets()


def trade_window_signal(ws, bal, bets, demo):
    end = ws + WINDOW
    markets = {}
    for coin, (_sym, pref) in COINS.items():
        mk = pm_market(pref, ws)
        if mk and mk.get("toks") and len(mk["toks"]) >= 2:
            markets[coin] = mk

    if not markets:
        log.info("Window %s: no markets", datetime.fromtimestamp(ws, timezone.utc).isoformat())
        return bal

    log.info(
        "Window %s [Signal Scalp]: macro 1h trend, TP +%.0f%%, hold up to %.1fh, trail -%.0f%% from peak",
        datetime.fromtimestamp(ws, timezone.utc).strftime("%H:%M"),
        SIG_TP * 100,
        SIG_MAX_HOLD_H,
        SIG_TRAIL_PCT * 100,
    )
    _append_trade_log({"kind": "window_start", "coin": "ALL", "t": ws, "price": 0, "side": "", "reason": f"Window {datetime.fromtimestamp(ws, timezone.utc).strftime('%Y-%m-%d %H:%M')}"})

    pos = {}
    cooldown = {}
    spent = 0.0
    last_hb = 0

    while time.time() < end - 2 and not _STOP_NOW:
        now = time.time()
        tr = int(end - now)

        if now - last_hb >= 120:
            last_hb = now
            open_packets = sum(len(p.open_packets()) for p in pos.values())
            log.info("  [I] T-%ds | positions %d | packets %d | exposure $%.2f", tr, len(pos), open_packets, spent)

        # ---- 0) Chart recording runs in a background thread; the trading
        # loop never waits on Binance/Polymarket chart snapshots. ----
        _snapshot_start(markets)

        # ---- 1) Exit checks: full exits, then phased partial exits ----
        for coin in list(pos.keys()):
            position = pos[coin]
            tok = markets[coin]["toks"][position.idx]
            bk = pm_book(tok)
            if not bk:
                continue
            # Use the REALISTIC (book-depth-walked) exit price for P&L, not the
            # naive top-of-book bid. Thin order books can have almost no size
            # at the top price, so a decision made off `bid` alone can look
            # fine (-20%) right up until the real fill lands far worse (-80%)
            # a few seconds later purely from slippage, not an actual price move.
            sell_est = pm_realistic_sell(tok, position.total_shares())
            exit_px = sell_est["vwap"] if sell_est else bk["bid"]
            held_h = (now - position.entry_t) / 3600.0
            agg_pnl = position.unrealized_pnl_pct(exit_px)

            position.trail_high = max(position.trail_high, exit_px)

            full_reason = None
            if agg_pnl >= SIG_TP:
                full_reason = "SIG-TP"
            elif held_h >= SIG_MAX_HOLD_H:
                full_reason = "SIG-MAX-HOLD"
            elif exit_px < SIG_HOPELESS_PRICE and end - now <= SIG_HOPELESS_MIN_LEFT * 60:
                # Hopeless late in the window: sell for whatever is left rather
                # than let the position ride to a full -100% at resolution.
                full_reason = "SIG-HOPELESS"
            else:
                sig = hourly_signal(COINS[coin][0])
                if sig and sig["side"] != position.side and held_h > SIG_MIN_HOLD_BEFORE_REVERSAL:
                    full_reason = "SIG-REVERSAL"
                elif held_h > SIG_MIN_HOLD_BEFORE_TRAIL and position.trail_high > position.last_buy():
                    # Trail is measured from the highest exit price the position
                    # ever saw and stays armed even after price slips below the
                    # entry -- a position that peaked +8% and reverses hard should
                    # not ride all the way to -100%. Once it has shown a real
                    # profit peak, a drop back to ~breakeven locks the gain in.
                    peak_pnl = (position.trail_high - position.last_buy()) / position.last_buy()
                    from_peak = (exit_px - position.trail_high) / position.trail_high
                    if peak_pnl >= SIG_LOCK_ARM_PCT and agg_pnl <= peak_pnl * SIG_LOCK_GIVE:
                        full_reason = "SIG-LOCK"
                    elif from_peak < -SIG_TRAIL_PCT:
                        full_reason = "SIG-TRAIL"

            if full_reason:
                # Full exit: close every remaining packet, don't leave anything open.
                for pkt in position.open_packets():
                    sell = pm_realistic_sell(tok, pkt["shares"])
                    if sell:
                        bal = _close_b(pkt, sell, full_reason, bal, bets)
                        pkt["status"] = "closed"
                        spent = max(0.0, spent - pkt["spent"])
                        _chart_event(coin, now, "exit", pkt.get("sell"), position.side, full_reason, pnl=pkt.get("pnl"))
                if position.is_flat():
                    cooldown[coin] = now + SIG_REENTRY_CD
                    del pos[coin]
                continue

            # Phased partial take-profit: lock in the best packet on a quick
            # profit spike, keep the rest riding the macro trend.
            if agg_pnl >= SIG_PARTIAL_TP_PCT and len(position.open_packets()) >= 2:
                pkt = position.best_open_packet(exit_px)
                sell = pm_realistic_sell(tok, pkt["shares"])
                if sell:
                    bal = _close_b(pkt, sell, "PARTIAL-TP", bal, bets)
                    pkt["status"] = "closed"
                    spent = max(0.0, spent - pkt["spent"])
                    position.partial_tp_stage += 1
                    _chart_event(coin, now, "exit", pkt.get("sell"), position.side, "PARTIAL-TP", pnl=pkt.get("pnl"))

            # Dynamic risk maneuvering: on a minor drawdown, trim only the
            # single worst packet instead of panic-closing the whole position.
            elif agg_pnl <= -SIG_PARTIAL_SL_PCT and len(position.open_packets()) >= 2:
                pkt = position.worst_open_packet(exit_px)
                sell = pm_realistic_sell(tok, pkt["shares"])
                if sell:
                    bal = _close_b(pkt, sell, "PARTIAL-SL", bal, bets)
                    pkt["status"] = "closed"
                    spent = max(0.0, spent - pkt["spent"])
                    position.partial_sl_stage += 1
                    _chart_event(coin, now, "exit", pkt.get("sell"), position.side, "PARTIAL-SL", pnl=pkt.get("pnl"))

        # ---- 2) Scale-in: add the next tranche to positions still deploying ----
        for coin, position in list(pos.items()):
            if end - now <= SIG_ENTRY_CUTOFF_MIN * 60:
                break  # entry blackout: no scale-in near window end
            if position.is_fully_deployed():
                continue
            if spent + position.tranche_stake > MAX_WINDOW_EXPOSURE:
                continue
            mk = markets.get(coin)
            if not mk:
                continue

            sig = hourly_signal(COINS[coin][0])
            if not sig or sig["side"] != position.side:
                continue  # trend no longer confirms -- don't add risk, let exit logic handle it

            tok = mk["toks"][position.idx]
            bk = pm_book(tok)
            if not bk:
                continue
            price_cap = _entry_price_cap(time.time(), ws)
            if bk["ask"] < SIG_PRICE_MIN or bk["ask"] > min(SIG_PRICE_MAX, price_cap) or bk["spread"] > SIG_SPREAD_MAX:
                continue

            last_buy = position.last_buy()
            adverse_move = (
                (bk["ask"] - last_buy) / last_buy
                if position.side == "UP"
                else (last_buy - bk["ask"]) / last_buy
            )
            if adverse_move > SIG_SCALE_MAX_ADVERSE:
                continue  # price spiked against us -- wait instead of averaging into volatility

            fill = pm_realistic_fill(tok, position.tranche_stake)
            if not fill or fill["spread"] > SIG_SPREAD_MAX:
                continue

            ex = execute(tok, position.side, fill["shares"], fill["vwap"], demo, amount=fill["spent"])
            live = (not demo) and (not DRY_RUN) and LIVE_TRADING
            if live and ex.get("status") != "live":
                if NOTIFY:
                    try:
                        NOTIFY("live_fail", {"coin": coin, "side": position.side, "info": ex})
                    except Exception:
                        pass
                continue

            pkt = position.add_packet(fill, ex, now, live)
            spent += fill["spent"]
            log.info(
                "  SCALE-IN [I] %s %s tranche %d/%d @%.3f $%.2f",
                coin,
                position.side,
                pkt["tranche"],
                position.max_tranches,
                pkt["buy"],
                pkt["spent"],
            )
            _chart_event(coin, now, "scale_in", pkt["buy"], position.side, "")
            if NOTIFY:
                try:
                    NOTIFY("sig_entry", pkt)
                except Exception:
                    pass

        # ---- 3) New entries: open a fresh phased position with tranche 1 ----
        first_tranche = STAKE / SIG_ENTRY_TRANCHES
        if end - now > SIG_ENTRY_CUTOFF_MIN * 60 and len(pos) < SIG_MAX_POS and spent + first_tranche <= MAX_WINDOW_EXPOSURE:
            for coin, mk in markets.items():
                if coin in pos:
                    continue
                if now < cooldown.get(coin, 0):
                    continue

                sig = hourly_signal(COINS[coin][0])
                if not sig:
                    continue

                idx = 0 if sig["side"] == "UP" else 1
                tok = mk["toks"][idx]
                bk = pm_book(tok)
                if not bk:
                    continue
                price_cap = _entry_price_cap(time.time(), ws)
                if bk["ask"] < SIG_PRICE_MIN or bk["ask"] > min(SIG_PRICE_MAX, price_cap):
                    log.info("  SKIP-ENTRY %s %s: ask %.2f outside [%.2f..%.2f] (%s)",
                             coin, sig["side"], bk["ask"], SIG_PRICE_MIN, min(SIG_PRICE_MAX, price_cap), sig["reason"])
                    continue
                if bk["spread"] > SIG_SPREAD_MAX:
                    log.info("  SKIP-ENTRY %s %s: spread %.2f > %.2f (%s)",
                             coin, sig["side"], bk["spread"], SIG_SPREAD_MAX, sig["reason"])
                    continue

                position = PhasedPosition(
                    coin, COINS[coin][1], ws, sig["side"], idx, mk["url"], mk["slug"],
                    round(sig["strength"], 3), sig["reason"], STAKE, SIG_ENTRY_TRANCHES,
                )
                fill = pm_realistic_fill(tok, position.tranche_stake)
                if not fill:
                    continue
                if fill["spread"] > SIG_SPREAD_MAX:
                    continue

                ex = execute(tok, sig["side"], fill["shares"], fill["vwap"], demo, amount=fill["spent"])
                live = (not demo) and (not DRY_RUN) and LIVE_TRADING
                if live and ex.get("status") != "live":
                    if NOTIFY:
                        try:
                            NOTIFY("live_fail", {"coin": coin, "side": sig["side"], "info": ex})
                        except Exception:
                            pass
                    continue

                pkt = position.add_packet(fill, ex, now, live)
                pos[coin] = position
                spent += fill["spent"]
                log.info(
                    "  ENTRY [I] %s %s tranche 1/%d @%.3f $%.2f | %s",
                    coin,
                    position.side,
                    position.max_tranches,
                    pkt["buy"],
                    pkt["spent"],
                    sig["reason"],
                )
                _chart_event(coin, now, "entry", pkt["buy"], position.side, sig["reason"])
                if NOTIFY:
                    try:
                        NOTIFY("sig_entry", pkt)
                    except Exception:
                        pass

                if len(pos) >= SIG_MAX_POS or spent + first_tranche > MAX_WINDOW_EXPOSURE:
                    break

        # Persist again so entry/exit events from this iteration are visible
        # to chart_gui.py immediately, not only after the next poll cycle.
        _persist_chart_data()

        # Periodic state save so crashes don't lose in-window bets.
        if _SAVE_CB:
            try:
                _SAVE_CB()
            except Exception:
                pass

        # Short sleep slices so STOP (_STOP_NOW) and window-end are noticed
        # within a couple seconds. The trader loop runs continuously -- chart
        # snapshots live in a background thread, so decision cycles are tight.
        sleep_left = SIG_POLL
        while sleep_left > 0 and not _STOP_NOW and time.time() < end - 2:
            step = min(2, sleep_left)
            time.sleep(step)
            sleep_left -= step

    for position in pos.values():
        for pkt in position.open_packets():
            pkt["result"] = "PENDING"
            pkt["real_outcome"] = "PENDING"
            pkt["pnl"] = 0.0
            pkt["settle_tries"] = 0
            bets.append(pkt)

    _append_trade_log({"kind": "window_end", "coin": "ALL", "t": ws, "price": 0, "side": "", "reason": f"Window {datetime.fromtimestamp(ws, timezone.utc).strftime('%Y-%m-%d %H:%M')}"})
    for coin in CHART_DATA:
        if CHART_DATA[coin].get("events"):
            _save_window_chart(coin, ws)
    return bal
