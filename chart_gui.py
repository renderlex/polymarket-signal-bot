"""
Live desktop chart window for the Polymarket Signal Scalp bot.

Fully independent process: reads `chart_data.json` (written periodically by
whale_agent.py's trade_window_signal loop) and renders a 2x3 grid, one panel
per coin, with Binance price vs Polymarket UP price and entry/exit markers.

Closing this window does NOT stop the trading bot -- it only stops the viewer.
Run standalone:  .venv\\Scripts\\python.exe chart_gui.py
"""
import json
import os
import sys
import tkinter as tk
from datetime import datetime

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

CHART_FILE = os.getenv("CHART_FILE", "chart_data.json")
REFRESH_MS = 3000
COINS = ["BTC", "ETH", "SOL", "XRP", "DOG", "BNB"]


def load_chart_data():
    try:
        with open(CHART_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


class ChartWindow:
    def __init__(self, root):
        self.root = root
        root.title("Polymarket Signal Scalp -- live chart (Binance vs Polymarket)")
        root.geometry("1400x820")

        self.fig, self.axes = plt.subplots(2, 3, figsize=(14, 8), dpi=100)
        self.fig.suptitle("Binance (orange) vs Polymarket UP bid (blue) -- \u25b2 entry/scale-in, \u25bc exit", fontsize=12)
        self.axes_flat = self.axes.flatten()
        self.twins = [ax.twinx() for ax in self.axes_flat]
        for ax, coin in zip(self.axes_flat, COINS):
            ax.set_title(coin, fontsize=11, fontweight="bold")

        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        status = tk.Label(root, text="Waiting for data...", anchor="w")
        status.pack(fill=tk.X)
        self.status = status

        self.refresh()

    def refresh(self):
        data = load_chart_data()
        any_data = False
        for ax, ax2, coin in zip(self.axes_flat, self.twins, COINS):
            ax.clear()
            ax2.clear()
            d = data.get(coin)
            ax.set_title(coin, fontsize=11, fontweight="bold")
            if not d or len(d.get("t", [])) < 2:
                ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes, color="#888")
                continue
            any_data = True
            times = [datetime.fromtimestamp(t) for t in d["t"]]
            ax2.plot(times, d["pm_bid"], color="#4dabf7", linewidth=1.6, label="Polymarket UP")
            ax2.set_ylim(0, 1)
            ax.plot(times, d["binance"], color="#ffa94d", linewidth=1.6, label="Binance")
            ax.tick_params(axis="x", labelrotation=30, labelsize=7)
            ax.tick_params(axis="y", labelsize=7)
            ax2.tick_params(axis="y", labelsize=7)

            for ev in d.get("events", []):
                price = ev.get("price")
                if price is None:
                    continue
                et = datetime.fromtimestamp(ev["t"])
                ax2.axvline(x=et, color="#868e96", linestyle="--", linewidth=0.4, alpha=0.3, zorder=2)
                if ev["kind"] in ("entry", "scale_in"):
                    label = "ENTRY" if ev["kind"] == "entry" else "scale-in"
                    ax2.scatter([et], [price], color="#37b24d", marker="^", s=100, zorder=6, edgecolors="white", linewidth=0.6)
                    ax2.annotate(f"\u25b2 {label} {ev['side']}@{price:.2f}", (et, price),
                                 xytext=(8, 8), textcoords="offset points", fontsize=7, color="#2f9e44",
                                 arrowprops=dict(arrowstyle="->", color="#2f9e44", lw=0.6))
                else:
                    ax2.scatter([et], [price], color="#e03131", marker="v", s=100, zorder=6, edgecolors="white", linewidth=0.6)
                    pnl_str = ""
                    if ev.get("pnl") is not None:
                        sgn = "+" if ev["pnl"] >= 0 else ""
                        pnl_str = f" {sgn}${ev['pnl']:.2f}"
                    ax2.annotate(f"\u25bc {ev.get('side','')} {ev.get('reason','')}@{price:.2f}{pnl_str}", (et, price),
                                 xytext=(8, -12), textcoords="offset points", fontsize=7, color="#c92a2a",
                                 arrowprops=dict(arrowstyle="->", color="#c92a2a", lw=0.6))

        self.fig.tight_layout(rect=(0, 0, 1, 0.95))
        self.canvas.draw_idle()
        self.status.config(
            text=("Updated: " + datetime.now().strftime("%H:%M:%S"))
            if any_data else "Waiting for the first data from the bot (needs at least one poll cycle)..."
        )
        self.root.after(REFRESH_MS, self.refresh)


def main():
    root = tk.Tk()
    ChartWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
