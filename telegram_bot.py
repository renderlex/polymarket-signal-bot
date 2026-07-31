"""
Telegram controller for Polymarket bot.
Single strategy only: Signal Scalp (I).
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import whale_agent as wa


LOCK = None
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
if not TOKEN or not CHAT:
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "dummy")
    CHAT = os.getenv("TELEGRAM_CHAT_ID", "0")
API = f"https://api.telegram.org/bot{TOKEN}"

# AUTO_MODE controls what happens on launch -- no more Start/Stop buttons,
# the bot starts trading automatically as soon as this script runs.
#   demo (default) -- safe paper trading, wa.DRY_RUN forced True.
#   live           -- real orders (requires PRIVATE_KEY + DRY_RUN=0/LIVE_TRADING=1 in .env).
AUTO_MODE = os.getenv("AUTO_MODE", "demo").strip().lower()

WINDOWS = [("4h", 14400), ("24h", 86400)]
STRATS = {"I": "[I] Signal Scalp (1h macro trend)"}

state = {
    "running": False,
    "demo": True,
    "live_armed": False,
    "strategy": "I",
    "window": int(os.getenv("WINDOW", "14400")),
    "stake": float(wa.STAKE),
    "bal": float(wa.DEMO_START),
    "start": float(wa.DEMO_START),
    "peak": float(wa.DEMO_START),
    "bets": [],
    "thread": None,
    "session": 0,
    "notify_entry": True,
}


def cprint(*parts):
    try:
        print(*parts, flush=True)
    except Exception:
        pass
    try:
        with open("telegram_bot.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in parts) + "\n")
    except Exception:
        pass


def single_instance_lock(port=50517):
    """Refuse to start a second instance. On Windows, SO_REUSEADDR (used by
    the old version of this function) lets a second unrelated process bind
    the SAME port successfully -- it does NOT protect against duplicates
    there like it does on Linux. SO_EXCLUSIVEADDRUSE is the correct flag on
    Windows to make a genuine conflict fail with a clear error instead of
    silently running two bots against the same state/API keys."""
    global LOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        LOCK = s
    except OSError as exc:
        raise SystemExit(
            f"Another instance of telegram_bot.py is already running (port {port} busy): {exc}"
        )


def hz_label(sec):
    for name, val in WINDOWS:
        if val == sec:
            return name
    return f"{sec}s"


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text):
    data = {"chat_id": CHAT, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(f"{API}/sendMessage", data=data, timeout=12)
    except Exception as e:
        cprint("send error", e)


def _kb_btn(text):
    return {"text": text}


def _keyboard():
    mode_lbl = "LIVE-dry" if not state["demo"] else "DEMO"
    run_lbl = "START" if not state["running"] else "STOP"
    return {
        "keyboard": [
            [_kb_btn("Status"), _kb_btn("Balance")],
            [_kb_btn("Mode " + mode_lbl), _kb_btn(run_lbl)],
            [_kb_btn("Help")],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }


def send_keyboard(text):
    data = {"chat_id": CHAT, "text": text, "reply_markup": json.dumps(_keyboard()), "parse_mode": "HTML"}
    try:
        requests.post(f"{API}/sendMessage", data=data, timeout=12)
    except Exception as e:
        cprint("send_keyboard error", e)


_LAST_UPDATE_ID = 0


def telegram_polling():
    global _LAST_UPDATE_ID
    while True:
        try:
            params = {"offset": _LAST_UPDATE_ID + 1, "timeout": 30, "allowed_updates": ["message", "callback_query"]}
            r = requests.get(f"{API}/getUpdates", params=params, timeout=35)
            if r.status_code != 200:
                time.sleep(5)
                continue
            for upd in r.json().get("result", []):
                _LAST_UPDATE_ID = upd["update_id"]
                cb = upd.get("callback_query")
                msg = cb.get("message", {}) if cb else upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                if str(chat_id) != CHAT:
                    continue
                text = (cb.get("data") or msg.get("text") or "").strip()
                if not text:
                    continue
                cmd = text.lower()
                if cmd in ("status", "/status"):
                    real, pending, mtm = fair_balance()
                    real_usdc = _real_usdc_display()
                    closed = [b for b in state["bets"] if b.get("result") in ("WIN", "LOSS")]
                    wins = [b for b in closed if b.get("result") == "WIN"]
                    losses = [b for b in closed if b.get("result") == "LOSS"]
                    wr = (len(wins) / len(closed) * 100) if closed else 0
                    run = "running" if state["running"] else "stopped"
                    mode = "LIVE" if (not state["demo"] and state["live_armed"]) else ("LIVE-dry" if not state["demo"] else "DEMO")
                    send_keyboard(
                        f"Strategy: {STRATS[state['strategy']]}\n"
                        f"Window: {hz_label(state['window'])} | Stake: ${state['stake']:.2f}\n"
                        f"Cash: ${state['bal']:.2f} | Fair: ${real:.2f}\n"
                        f"Real USDC: {real_usdc}\n"
                        f"Open: {pending} (mtm +${mtm:.2f})\n"
                        f"Closed: {len(closed)} | WIN: {len(wins)} LOSS: {len(losses)} ({wr:.0f}%)\n"
                        f"Mode: {mode} | Bot: {run}"
                    )
                elif cmd in ("balance", "/balance"):
                    real_usdc = _real_usdc_display()
                    send_keyboard(f"Demo balance: ${state['bal']:.2f}\nReal USDC: {real_usdc}")
                elif cmd.startswith("mode") or cmd in ("/mode",):
                    if state["running"]:
                        send_keyboard("Stop the bot first, then change mode.")
                        continue
                    current = state["demo"]
                    state["demo"] = not current
                    state["live_armed"] = False
                    mode = "DEMO" if state["demo"] else "LIVE-dry"
                    send_keyboard(f"Mode: {mode}. Run Start to apply.")
                elif cmd in ("start", "/start"):
                    if not state["running"]:
                        start_bot()
                    send_keyboard("Bot started." if state["running"] else "Start failed.")
                elif cmd in ("stop", "/stop"):
                    stop_bot()
                    send_keyboard("Bot stopped.")
                elif cmd in ("help", "/help", "/h"):
                    send_keyboard(
                        "<b>Status</b> — balance, positions, mode\n"
                        "<b>Balance</b> — demo vs real USDC\n"
                        "<b>Mode</b> — switch demo/live\n"
                        "<b>Start/Stop</b> — bot control"
                    )
        except Exception:
            time.sleep(5)


def _real_usdc_display():
    if state["demo"] or not os.getenv("PRIVATE_KEY"):
        return "N/A (demo)"
    try:
        bal = wa.usdc_balance()
        return f"${bal:.2f}"
    except Exception:
        return "N/A (no keys)"


def status_text():
    real, pending, mtm = fair_balance()
    closed = [b for b in state["bets"] if b.get("result") in ("WIN", "LOSS")]
    wins = [b for b in closed if b.get("result") == "WIN"]
    losses = [b for b in closed if b.get("result") == "LOSS"]
    wr = (len(wins) / len(closed) * 100) if closed else 0
    run = "running" if state["running"] else "stopped"
    mode = "LIVE" if (not state["demo"] and state["live_armed"]) else ("LIVE-dry" if not state["demo"] else "DEMO")
    rusdc = _real_usdc_display()
    return (
        f"Strategy: <b>{STRATS[state['strategy']]}</b>\n"
        f"Window: <b>{hz_label(state['window'])}</b> | Stake: <b>${state['stake']:.2f}</b>\n"
        f"Cash: <b>${state['bal']:.2f}</b> | Fair: <b>${real:.2f}</b>\n"
        f"Real USDC: <b>{rusdc}</b>\n"
        f"Open: <b>{pending}</b> (mtm +${mtm:.2f})\n"
        f"Closed: <b>{len(closed)}</b> | WIN: <b>{len(wins)}</b> LOSS: <b>{len(losses)}</b> ({wr:.0f}%)\n"
        f"Mode: <b>{mode}</b> | Bot: <b>{run}</b>"
    )


def fair_balance():
    try:
        nb, settled = wa.settle_pending(state["bets"], state["bal"])
        if settled:
            state["bal"] = nb
            save_state()
    except Exception:
        pass

    pending = [b for b in state["bets"] if b.get("result") == "PENDING"]
    mtm = 0.0
    for b in pending:
        try:
            mk = wa.pm_market(b["prefix"], b["ws"])
            if not mk:
                continue
            tok = mk["toks"][b.get("idx", 0 if b.get("side") == "UP" else 1)]
            bk = wa.pm_book(tok)
            if bk:
                mtm += b["shares"] * bk["bid"]
        except Exception:
            continue
    return state["bal"] + mtm, len(pending), mtm


def on_event(kind, payload):
    if kind == "sig_entry" and state["notify_entry"]:
        send(
            f"ENTRY <b>{payload.get('coin')} {payload.get('side')}</b> @ {payload.get('buy')}\n"
            f"Spent: ${payload.get('spent',0):.2f}\n"
            f"{esc(payload.get('ai_reason',''))}"
        )
    elif kind == "b_exit":
        send(
            f"EXIT <b>{payload.get('coin')} {payload.get('side')}</b>\n"
            f"{esc(payload.get('exit_reason',''))} | PnL {payload.get('pnl',0):+.2f}\n"
            f"Balance: ${payload.get('balance', state['bal']):.2f}"
        )
    elif kind == "b_settle":
        send(
            f"SETTLE <b>{payload.get('coin')} {payload.get('side')}</b> -> {payload.get('result','?')}\n"
            f"PnL {payload.get('pnl',0):+.2f}\n"
            f"Balance: ${payload.get('balance', state['bal']):.2f}"
        )
    elif kind == "live_fail":
        info = payload.get("info", {})
        send(
            f"Order failed: <b>{payload.get('coin')} {payload.get('side')}</b>\n"
            f"{esc(info.get('status'))} {esc(info.get('error') or info.get('resp') or '')}"
        )


wa.NOTIFY = on_event


def apply_window(sec):
    state["window"] = int(sec)
    wa.WINDOW = int(sec)
    wa.SUFFIX = {300: "5m", 900: "15m", 14400: "4h", 86400: "1d"}.get(int(sec), "4h")


def apply_stake(amt):
    state["stake"] = float(amt)
    wa.STAKE = float(amt)


def apply_live_flags():
    live = (not state["demo"]) and state["live_armed"]
    wa.DRY_RUN = not live
    wa.LIVE_TRADING = live


def save_state():
    try:
        with open("telegram_state.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "balance": state["bal"],
                    "bets": state["bets"],
                    "window": state["window"],
                    "stake": state["stake"],
                    "strategy": state["strategy"],
                    "ws": state.get("ws"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        cprint("save_state error", e)


def load_state():
    try:
        with open("telegram_state.json", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            state["bal"] = float(d.get("balance", state["bal"]))
            state["bets"] = d.get("bets", state["bets"])
            state["window"] = int(d.get("window", state["window"]))
            state["stake"] = float(d.get("stake", state["stake"]))
            state["strategy"] = d.get("strategy", "I")
            state["ws"] = d.get("ws")
            state["start"] = state["bal"]
            state["peak"] = state["bal"]
            return len(state["bets"])
    except FileNotFoundError:
        return 0
    except Exception as e:
        cprint("load_state error", e)
    return 0


def trader_loop(session_id):
    cprint("Trader loop started (session %d)" % session_id)
    last_ws = None
    while state["running"] and state["session"] == session_id:
        try:
            nb, settled = wa.settle_pending(state["bets"], state["bal"])
            if settled:
                state["bal"] = nb
                save_state()
        except Exception as e:
            cprint("settle error", e)

        fair, _, _ = fair_balance()
        state["peak"] = max(state["peak"], fair)
        if wa.risk_stop(fair, state["peak"], state["start"]):
            send("STOP by risk limits")
            state["running"] = False
            break

        live = (not state["demo"]) and state["live_armed"]
        if live:
            try:
                if wa.usdc_balance() < state["stake"]:
                    send("Insufficient USDC. Bot stopped.")
                    state["running"] = False
                    break
            except Exception as e:
                send(f"USDC check failed: {esc(e)}")

        now = int(time.time())
        ws = (now // wa.WINDOW) * wa.WINDOW
        if ws + wa.WINDOW - now < 5:
            ws += wa.WINDOW
        if ws == last_ws:
            time.sleep(3)
            continue

        last_ws = ws
        state["ws"] = ws
        save_state()
        try:
            state["bal"] = wa.trade_window_signal(ws, state["bal"], state["bets"], state["demo"])
            save_state()
        except Exception as e:
            send(f"Window error: {esc(e)}")
            time.sleep(5)

    send_keyboard("Bot stopped.")


def _autosave_loop():
    """Periodically save state so crashes don't lose in-window bets."""
    while True:
        time.sleep(30)
        try:
            save_state()
        except Exception:
            pass


def start_bot():
    if state["running"]:
        cprint("Already running")
        return

    if not state["demo"] and not os.getenv("PRIVATE_KEY"):
        send("LIVE unavailable: missing PRIVATE_KEY. Falling back to DEMO.")
        state["demo"] = True
        state["live_armed"] = False

    apply_window(state["window"])
    apply_stake(state["stake"])
    apply_live_flags()

    fair, _, _ = fair_balance()
    state["start"] = fair
    state["peak"] = fair
    state["session"] += 1
    state["running"] = True
    wa._STOP_NOW = False
    state["thread"] = threading.Thread(target=trader_loop, args=(state["session"],), daemon=True)
    state["thread"].start()

    mode = "DEMO" if state["demo"] else ("LIVE" if state["live_armed"] else "LIVE-dry")
    cprint(f"Bot started: window={hz_label(state['window'])} stake=${state['stake']:.2f} mode={mode}")
    send_keyboard(
        f"Started <b>{STRATS['I']}</b>\n"
        f"Window: {hz_label(state['window'])} | Stake: ${state['stake']:.2f} | Mode: {mode}"
    )


def stop_bot():
    if not state["running"]:
        return
    state["running"] = False
    state["live_armed"] = False
    wa._STOP_NOW = True
    send_keyboard("Bot stopped.")


def launch_chart_gui():
    """Spawn the desktop chart window (chart_gui.py) as an independent process.
    It only reads chart_data.json -- closing it never affects the running bot."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "chart_gui.py")
    if not os.path.exists(script):
        cprint("chart_gui.py not found, skipping chart window")
        return
    try:
        subprocess.Popen([sys.executable, script], cwd=here)
        cprint("Chart window launched (chart_gui.py)")
    except Exception as e:
        cprint("Failed to launch chart window:", e)


def main():
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    cprint("=" * 50)
    cprint("  Polymarket Bot -- Signal Scalp (1h macro trend)")
    cprint("  Telegram = notifications only. Chart = separate window.")
    cprint("=" * 50)

    single_instance_lock()
    cprint("Lock acquired (port 50517) -- single instance OK")

    n = load_state()
    if n:
        pending = [b for b in state["bets"] if b.get("result") == "PENDING"]
        cprint(f"State restored: {n} bets ({len(pending)} pending), balance ${state['bal']:.2f}")
        if pending:
            send(f"Recovered {len(pending)} pending bets from crash. Bot will settle them on next window.")
    apply_window(state["window"])
    apply_stake(state["stake"])

    wa._SAVE_CB = save_state

    launch_chart_gui()

    cprint("Starting Telegram command polling...")
    threading.Thread(target=telegram_polling, daemon=True).start()
    threading.Thread(target=_autosave_loop, daemon=True).start()

    state["demo"] = AUTO_MODE != "live"
    state["live_armed"] = False
    if AUTO_MODE == "live":
        try:
            wa.get_real_client()
            state["live_armed"] = True
        except Exception as e:
            cprint("LIVE init failed, falling back to DEMO:", e)
            state["demo"] = True

    try:
        send(
            f"Bot ready. Strategy: <b>{STRATS['I']}</b>\n"
            f"Window: {hz_label(state['window'])} | Stake: ${state['stake']:.2f}\n"
            f"Auto-starting ({'LIVE' if state['live_armed'] else 'DEMO'})..."
        )
        cprint("Telegram startup message sent OK")
    except Exception as e:
        cprint("Telegram startup message FAILED:", e)

    start_bot()
    cprint("Bot running automatically. This window is informational only -- Ctrl+C to stop.")

    last_heartbeat = time.time()
    try:
        while True:
            time.sleep(3)
            if time.time() - last_heartbeat >= 300:
                last_heartbeat = time.time()
                real, pending, mtm = fair_balance()
                rusdc = _real_usdc_display()
                mode = "LIVE" if (not state["demo"] and state["live_armed"]) else ("LIVE-dry" if not state["demo"] else "DEMO")
                cprint(f"[{mode}] bal=${state['bal']:.2f} fair=${real:.2f} usdc={rusdc} open={pending} running={state['running']}")
    except KeyboardInterrupt:
        stop_bot()
        cprint("Shutting down...")
    finally:
        try:
            requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": "Bot offline."}, timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    main()

