"""
algo_bot_dashboard.py  -  Paper-trading algo bot + dashboard (no API keys, no real money)

MODES      : Crypto (BTC/ETH/SOL, Binance Futures, 24x7)
             Indian Stocks (Nifty 50 index + large caps, Yahoo Finance, NSE hours 9:15-15:30 IST)
TIMEFRAMES : 5m or 15m  (switch any time from the dashboard)

STRATEGY: Trade Like Berlin "Liquidity Sweep + Confirmation" (order psychology)
  1. Liquidity zones : swing highs / swing lows (pivots) where retail stop-losses rest.
  2. Accumulation    : price compresses (tight range) before the sweep.
  3. Sweep           : a candle wicks THROUGH the zone but CLOSES back inside.
                       Wick below a swing low  -> bullish setup
                       Wick above a swing high -> bearish setup
  4. Confirmation    : within 3 candles a candle closes beyond the sweep candle's high/low.
  5. Stop loss       : beyond the sweep wick (+0.2 ATR buffer).   6. Take profit: 2R.
  Risk: 1% of equity per trade, max 3 open trades, 1 per symbol, 3% daily loss limit, 5x leverage cap.

INDIAN STOCK RULES (intraday paper trading, like MIS):
  - trades only while NSE is open (Mon-Fri 9:15-15:30 IST, holidays detected by stale data)
  - no new entries before 9:30 or after 14:45 IST
  - every open trade is auto squared-off at 15:15 IST
  - whole shares only (Nifty 50 index is traded in index units = paper "points exposure")

RUN : python algo_bot_dashboard.py   (standard Python 3.8+ with tkinter)
"""

import json
import csv
import math
import os
import queue
import threading
import time
try:                          # tkinter is optional so the same bot can run headless / on phone
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    tk = ttk = None
from urllib.request import urlopen, Request
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

# ----------------------------- SETTINGS -----------------------------
TIMEFRAMES = ["5m", "15m"]
DEFAULT_MODE, DEFAULT_TF = "crypto", "15m"

PROFILES = {
    "crypto": dict(
        label="Crypto", source="binance", currency="USDT",
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        start_equity=10000.0, fee=0.0005, slip=0.0002, poll=10, session=None),
    "india": dict(
        label="Indian Stocks", source="yahoo", currency="INR",
        # Edit this list to add/remove stocks. NSE stocks end with .NS, Nifty 50 index is ^NSEI
        symbols=["^NSEI", "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "TCS.NS",
                 "INFY.NS", "SBIN.NS", "ITC.NS", "BHARTIARTL.NS"],
        start_equity=1000000.0, fee=0.0003, slip=0.0002, poll=30,
        session=dict(open=(9, 15), close=(15, 30), entry_from=(9, 30),
                     entry_to=(14, 45), square_off=(15, 15))),
}

RISK_PER_TRADE = 0.01        # 1% of equity
RR = 2.0                     # take profit = 2R
ATR_LEN = 14
PIVOT_LEN = 5                # candles each side to confirm a swing high/low
LIQ_LOOKBACK = 120           # how far back to look for liquidity zones
CONFIRM_WINDOW = 3           # candles allowed between sweep and confirmation
ACCUM_BARS = 8               # candles before the sweep checked for compression
ACCUM_ATR = 3.0              # range of those candles must be <= this many ATR
SL_BUFFER = 0.2              # extra ATR beyond the sweep wick
MAX_STOP_ATR = 4.0           # skip setups with a stop wider than this
MAX_OPEN = 3
DAILY_LOSS_LIMIT = 0.03
MAX_LEVERAGE = 5.0
SETTINGS_FILE = "algo_bot_settings.json"
STALE_MS = 45 * 60 * 1000    # candle older than this during market hours = holiday / dead feed
# --------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
BINANCE = "https://fapi.binance.com/fapi/v1/klines"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0"}


def sym_label(sym):
    if sym == "^NSEI":
        return "NIFTY 50"
    return sym[:-3] if sym.endswith(".NS") else sym


# ------------------------- indicators -------------------------
def atr(h, l, c, n):
    trs = [h[0] - l[0]]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    a, out = sum(trs[:n]) / n, []
    for i, t in enumerate(trs):
        a = (a * (n - 1) + t) / n if i >= n else a
        out.append(a)
    return out


# ------------------------- data feeds -------------------------
def fetch_binance(symbol, interval, limit=320):
    req = Request(f"{BINANCE}?symbol={symbol}&interval={interval}&limit={limit}", headers=UA)
    with urlopen(req, timeout=10) as r:
        raw = json.loads(r.read().decode())
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]), l=float(k[3]),
                 c=float(k[4]), v=float(k[5])) for k in raw]


def fetch_yahoo(symbol, interval):
    rng = "5d" if interval == "5m" else "10d"
    req = Request(f"{YAHOO}{quote(symbol, safe='')}?interval={interval}&range={rng}", headers=UA)
    with urlopen(req, timeout=12) as r:
        j = json.loads(r.read().decode())
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append(dict(t=int(t) * 1000, o=o, h=h, l=l, c=c, v=q["volume"][i] or 0))
    return out


def fetch(source, symbol, interval):
    return fetch_yahoo(symbol, interval) if source == "yahoo" else fetch_binance(symbol, interval)


# --------------------------- the bot ---------------------------
class PaperBot:
    def __init__(self, ui_queue, mode=None, tf=None):
        self.q = ui_queue
        self.lock = threading.RLock()
        self.running = False
        self.mode = None
        self.tf = DEFAULT_TF
        self.session = "-"
        saved = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                saved = json.load(open(SETTINGS_FILE))
            except Exception:
                pass
        m = mode or saved.get("mode") or DEFAULT_MODE
        self.tf = tf or saved.get("tf") or DEFAULT_TF
        if m not in PROFILES:
            m = DEFAULT_MODE
        if self.tf not in TIMEFRAMES:
            self.tf = DEFAULT_TF
        self._enter_mode(m)

    # ---------- mode / timeframe ----------
    def _enter_mode(self, mode):
        self.mode = mode
        self.prof = PROFILES[mode]
        self.equity = self.start_equity = self.peak = self.day_start_equity = self.prof["start_equity"]
        self.max_dd = 0.0
        self.positions, self.trades = {}, []
        self.market = {}
        self.last_signal_candle = {}
        self.paused_today = False
        self.day = self.day_key()
        self.session = "-"
        self.load()

    def switch(self, mode=None, tf=None):
        """Change market mode and/or timeframe. Returns an error message or ''."""
        with self.lock:
            if mode and mode not in PROFILES:
                return "Unknown mode"
            if tf and tf not in TIMEFRAMES:
                return "Unknown timeframe"
            if mode and mode != self.mode:
                if self.positions:
                    return "Close all open trades before switching market mode"
                self.save()
                self.running = False
                self._enter_mode(mode)
                self.log(f"Mode -> {self.prof['label']} (bot stopped, press START)")
            if tf and tf != self.tf:
                self.tf = tf
                self.market = {}
                self.last_signal_candle = {}
                self.log(f"Timeframe -> {tf}")
            json.dump(dict(mode=self.mode, tf=self.tf), open(SETTINGS_FILE, "w"))
            return ""

    # ---------- persistence ----------
    def state_file(self):
        return f"algo_bot_state_{self.mode}.json"

    def trades_csv(self):
        return f"algo_bot_trades_{self.mode}.csv"

    def load(self):
        path = self.state_file()
        if not os.path.exists(path) and self.mode == "crypto" and os.path.exists("algo_bot_state.json"):
            path = "algo_bot_state.json"          # state from the older single-mode version
        if os.path.exists(path):
            try:
                s = json.load(open(path))
                self.equity = s["equity"]; self.peak = s["peak"]; self.max_dd = s["max_dd"]
                self.positions = s["positions"]; self.trades = s["trades"]
                self.start_equity = s.get("start_equity", self.start_equity)
                self.day_start_equity = s.get("day_start_equity", self.equity)
                self.day = s.get("day", self.day)
            except Exception:
                pass

    def save(self):
        json.dump(dict(equity=self.equity, peak=self.peak, max_dd=self.max_dd,
                       positions=self.positions, trades=self.trades,
                       start_equity=self.start_equity,
                       day_start_equity=self.day_start_equity, day=self.day),
                  open(self.state_file(), "w"))

    def log(self, msg):
        self.q.put(("log", f"{datetime.now().strftime('%H:%M:%S')}  {msg}"))

    # ---------- user actions ----------
    def toggle(self):
        with self.lock:
            self.running = not self.running
            self.log("Bot STARTED" if self.running else "Bot STOPPED (open trades still managed)")

    def close_all(self):
        with self.lock:
            for s in list(self.positions):
                m = self.market.get(s)
                if m:
                    self.close_trade(s, m["price"], "MANUAL")
            self.save()

    def reset_account(self):
        with self.lock:
            if os.path.exists(self.state_file()):
                os.remove(self.state_file())
            self.running = False
            self._enter_mode(self.mode)
            self.log("Paper account reset")

    # ---------- time helpers ----------
    def day_key(self):
        if self.prof.get("session"):
            return datetime.now(IST).date().isoformat()
        return datetime.now(timezone.utc).date().isoformat()

    def session_state(self, live):
        s = self.prof.get("session")
        if not s:
            return "24x7"
        n = datetime.now(IST)
        if n.weekday() >= 5:
            return "CLOSED"
        t = (n.hour, n.minute)
        if not (s["open"] <= t < s["close"]):
            return "CLOSED"
        if time.time() * 1000 - live["t"] > STALE_MS:
            return "CLOSED"
        return "OPEN"

    def new_day_check(self):
        today = self.day_key()
        if today != self.day:
            self.day, self.day_start_equity, self.paused_today = today, self.equity, False
            self.log("New trading day - daily loss counter reset")

    # ---------- trading ----------
    def size(self, sym, entry, stop_dist):
        qty = (self.equity * RISK_PER_TRADE) / stop_dist
        qty = min(qty, self.equity * MAX_LEVERAGE / entry)         # leverage cap
        if self.prof["source"] == "yahoo" and not sym.startswith("^"):
            qty = math.floor(qty)                                  # whole shares only
        return qty

    def open_trade(self, sym, side, price, sl_level, candle_t):
        slip = self.prof["slip"]
        entry = price * (1 + slip if side == "LONG" else 1 - slip)
        stop_dist = abs(entry - sl_level)
        if stop_dist <= 0:
            return
        qty = self.size(sym, entry, stop_dist)
        if qty <= 0:
            self.log(f"SKIP {sym_label(sym)} {side}: position size below 1 share for 1% risk")
            return
        tp = entry + RR * stop_dist if side == "LONG" else entry - RR * stop_dist
        fee = qty * entry * self.prof["fee"]
        self.equity -= fee
        self.positions[sym] = dict(side=side, entry=entry, sl=sl_level, tp=tp, qty=qty,
                                   fee_in=fee, t_open=time.time(), candle_t=candle_t,
                                   risk=qty * stop_dist,
                                   date=datetime.now(IST).date().isoformat())
        self.log(f"OPEN {side} {sym_label(sym)} @ {entry:.2f}  SL {sl_level:.2f}  TP {tp:.2f}  qty {qty:g}")

    def close_trade(self, sym, price, reason):
        p = self.positions.pop(sym)
        slip = self.prof["slip"]
        exit_p = price * (1 - slip if p["side"] == "LONG" else 1 + slip) if reason != "TP" else price
        direction = 1 if p["side"] == "LONG" else -1
        gross = (exit_p - p["entry"]) * p["qty"] * direction
        fee_out = p["qty"] * exit_p * self.prof["fee"]
        net = gross - fee_out - p["fee_in"]
        self.equity += gross - fee_out          # entry fee already deducted
        self.peak = max(self.peak, self.equity)
        self.max_dd = max(self.max_dd, (self.peak - self.equity) / self.peak * 100)
        r_mult = net / p["risk"] if p["risk"] else 0
        tr = dict(time=datetime.now().strftime("%Y-%m-%d %H:%M"), symbol=sym_label(sym),
                  side=p["side"], entry=round(p["entry"], 4), exit=round(exit_p, 4),
                  qty=round(p["qty"], 5), pnl=round(net, 2), r=round(r_mult, 2), reason=reason)
        self.trades.append(tr)
        path = self.trades_csv()
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=tr.keys())
            if new:
                w.writeheader()
            w.writerow(tr)
        self.log(f"CLOSE {p['side']} {sym_label(sym)} @ {exit_p:.2f} [{reason}]  PnL {net:+.2f} ({r_mult:+.2f}R)")

    # ---------- strategy ----------
    @staticmethod
    def pivots(h, l):
        """Swing highs / lows confirmed by PIVOT_LEN candles on each side."""
        n, P = len(h), PIVOT_LEN
        highs, lows = [], []
        for i in range(max(P, n - LIQ_LOOKBACK), n - P):
            if h[i] == max(h[i - P:i + P + 1]):
                highs.append((i, h[i]))
            if l[i] == min(l[i - P:i + P + 1]):
                lows.append((i, l[i]))
        return highs, lows

    def find_setup(self, o, h, l, c, a):
        """Return (side, sl_level, text) if a sweep + confirmation just completed."""
        n = len(c)
        highs, lows = self.pivots(h, l)
        for back in range(1, CONFIRM_WINDOW + 1):
            j = n - 1 - back
            seg_h = max(h[j - ACCUM_BARS:j]); seg_l = min(l[j - ACCUM_BARS:j])
            accum = (seg_h - seg_l) <= ACCUM_ATR * a
            for i, lvl in lows:                       # bullish: sell-side liquidity swept
                if i <= j - PIVOT_LEN and l[j] < lvl and c[j] > lvl:
                    if c[-1] > h[j] and accum and min(l[j:]) >= l[j]:
                        return "LONG", l[j] - SL_BUFFER * a, f"sell-side sweep @ {lvl:.2f} + confirm"
            for i, lvl in highs:                      # bearish: buy-side liquidity swept
                if i <= j - PIVOT_LEN and h[j] > lvl and c[j] < lvl:
                    if c[-1] < l[j] and accum and max(h[j:]) <= h[j]:
                        return "SHORT", h[j] + SL_BUFFER * a, f"buy-side sweep @ {lvl:.2f} + confirm"
        return None, None, "-"

    def process(self, sym, k):
        """Handle one symbol's candles (call with self.lock held)."""
        live, closed = k[-1], k[:-1]
        o = [x["o"] for x in closed]; c = [x["c"] for x in closed]
        h = [x["h"] for x in closed]; l = [x["l"] for x in closed]
        if len(c) < LIQ_LOOKBACK:
            self.market[sym] = dict(label=sym_label(sym), price=live["c"], near_lo=None, near_hi=None,
                                    accum="-", atr=0, signal="not enough candles", upnl=0)
            return
        a = atr(h, l, c, ATR_LEN)[-1]
        price = live["c"]
        session = self.session_state(live)
        self.session = session
        sess_cfg = self.prof.get("session")
        highs, lows = self.pivots(h, l)
        below = [v for _, v in lows if v < price]
        above = [v for _, v in highs if v > price]
        near_lo = max(below) if below else None
        near_hi = min(above) if above else None
        accum_now = (max(h[-ACCUM_BARS:]) - min(l[-ACCUM_BARS:])) <= ACCUM_ATR * a

        pos = self.positions.get(sym)
        now = datetime.now(IST)

        # --- intraday square-off (Indian stocks)
        if pos and sess_cfg:
            if (now.hour, now.minute) >= sess_cfg["square_off"] or pos.get("date") != now.date().isoformat():
                self.close_trade(sym, price, "SQUARE-OFF")
                pos = None

        # --- manage SL / TP
        if pos and session != "CLOSED" and live["t"] >= pos["candle_t"]:
            hit = None
            if pos["side"] == "LONG":
                if live["l"] <= pos["sl"]: hit = ("SL", pos["sl"])
                elif live["h"] >= pos["tp"]: hit = ("TP", pos["tp"])
            else:
                if live["h"] >= pos["sl"]: hit = ("SL", pos["sl"])
                elif live["l"] <= pos["tp"]: hit = ("TP", pos["tp"])
            if hit:
                self.close_trade(sym, hit[1], hit[0])
                pos = None

        # --- new entry
        side, sl_level, text = self.find_setup(o, h, l, c, a)
        entry_window = True
        if sess_cfg:
            tt = (now.hour, now.minute)
            entry_window = session == "OPEN" and sess_cfg["entry_from"] <= tt < sess_cfg["entry_to"]
        if (not pos and side and self.running and not self.paused_today and entry_window
                and len(self.positions) < MAX_OPEN
                and self.last_signal_candle.get(sym) != closed[-1]["t"]):
            self.last_signal_candle[sym] = closed[-1]["t"]
            if abs(price - sl_level) <= MAX_STOP_ATR * a:
                self.open_trade(sym, side, price, sl_level, live["t"])
            else:
                self.log(f"SKIP {sym_label(sym)} {side}: stop wider than {MAX_STOP_ATR:g} ATR")

        upnl = 0.0
        if sym in self.positions:
            p = self.positions[sym]
            upnl = (price - p["entry"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)
        status = "market closed" if session == "CLOSED" else (f"{side}: {text}" if side else "waiting for sweep")
        self.market[sym] = dict(label=sym_label(sym), price=price, near_lo=near_lo, near_hi=near_hi,
                                accum="YES" if accum_now else "no", atr=a, signal=status, upnl=upnl)

    def total_equity(self):
        eq = self.equity
        for sym, p in self.positions.items():
            m = self.market.get(sym)
            if m:
                eq += (m["price"] - p["entry"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)
        return eq

    def loop(self):
        while True:
            poll = 10
            try:
                with self.lock:
                    self.new_day_check()
                    mode, tf = self.mode, self.tf
                    src, syms, poll = self.prof["source"], list(self.prof["symbols"]), self.prof["poll"]
                for s in syms:
                    try:
                        k = fetch(src, s, tf)
                    except Exception as e:
                        self.log(f"{sym_label(s)} data error: {e}")
                        continue
                    with self.lock:
                        if (self.mode, self.tf) != (mode, tf):
                            break                          # user switched while we were fetching
                        self.process(s, k)
                with self.lock:
                    te = self.total_equity()
                    if not self.paused_today and (self.day_start_equity - te) / self.day_start_equity >= DAILY_LOSS_LIMIT:
                        self.paused_today = True
                        self.log(f"DAILY LOSS LIMIT {DAILY_LOSS_LIMIT*100:.0f}% hit - no new trades today")
                    self.save()
                self.q.put(("refresh", None))
            except Exception as e:
                self.log(f"loop error: {e}")
            time.sleep(poll)


# ---------------------------- tkinter dashboard ----------------------------
class Dashboard:
    def __init__(self, root):
        self.root = root
        root.title("Algo Bot - Paper Trading Dashboard")
        root.geometry("1100x800")
        root.configure(bg="#10131a")
        self.q = queue.Queue()
        self.bot = PaperBot(self.q)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#181c26", fieldbackground="#181c26",
                        foreground="#e6e6e6", rowheight=24, borderwidth=0)
        style.configure("Treeview.Heading", background="#232938", foreground="#ffffff",
                        font=("Segoe UI", 9, "bold"))

        top = tk.Frame(root, bg="#10131a"); top.pack(fill="x", padx=10, pady=8)
        self.stat = {}
        for name in ["Equity", "Total PnL", "Today PnL", "Win rate", "Trades", "Max DD", "Status", "Market"]:
            f = tk.Frame(top, bg="#181c26", padx=10, pady=6); f.pack(side="left", padx=3, fill="x", expand=True)
            tk.Label(f, text=name, fg="#8b93a7", bg="#181c26", font=("Segoe UI", 8)).pack()
            lb = tk.Label(f, text="-", fg="#ffffff", bg="#181c26", font=("Segoe UI", 13, "bold")); lb.pack()
            self.stat[name] = lb

        ctl = tk.Frame(root, bg="#10131a"); ctl.pack(fill="x", padx=10)
        tk.Label(ctl, text="Market:", fg="#8b93a7", bg="#10131a").pack(side="left", padx=(4, 2))
        self.mode_var = tk.StringVar(value=PROFILES[self.bot.mode]["label"])
        mode_cb = ttk.Combobox(ctl, textvariable=self.mode_var, state="readonly", width=14,
                               values=[p["label"] for p in PROFILES.values()])
        mode_cb.pack(side="left"); mode_cb.bind("<<ComboboxSelected>>", self.on_mode)
        tk.Label(ctl, text="Timeframe:", fg="#8b93a7", bg="#10131a").pack(side="left", padx=(10, 2))
        self.tf_var = tk.StringVar(value=self.bot.tf)
        tf_cb = ttk.Combobox(ctl, textvariable=self.tf_var, state="readonly", width=5, values=TIMEFRAMES)
        tf_cb.pack(side="left"); tf_cb.bind("<<ComboboxSelected>>", self.on_tf)
        self.btn = tk.Button(ctl, text="START BOT", bg="#1f9d55", fg="white", width=14,
                             font=("Segoe UI", 10, "bold"), command=self.toggle)
        self.btn.pack(side="left", padx=(14, 4))
        tk.Button(ctl, text="Close all", bg="#c0392b", fg="white", width=10, command=self.close_all).pack(side="left", padx=4)
        tk.Button(ctl, text="Reset paper account", bg="#444b5e", fg="white", command=self.reset).pack(side="left", padx=4)
        self.info = tk.Label(ctl, text="", fg="#8b93a7", bg="#10131a"); self.info.pack(side="right")

        self.mk = self.table("Market scan", ["Symbol", "Price", "Liq below", "Liq above", "Accumulation", "ATR", "Signal"], 6)
        self.ps = self.table("Open positions", ["Symbol", "Side", "Entry", "SL", "TP", "Qty", "Unrealised PnL"], 4)
        self.hs = self.table("Trade history", ["Time", "Symbol", "Side", "Entry", "Exit", "PnL", "R", "Reason"], 8)
        self.logbox = tk.Text(root, height=7, bg="#0b0d12", fg="#9fb3c8", font=("Consolas", 9), borderwidth=0)
        self.logbox.pack(fill="x", padx=10, pady=6)
        self.logbox.insert("end", "Bot ready. Pick market + timeframe, then press START BOT.\n")

        threading.Thread(target=self.bot.loop, daemon=True).start()
        self.poll()

    def table(self, title, cols, height):
        tk.Label(self.root, text=title, fg="#ffffff", bg="#10131a", font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(6, 0))
        t = ttk.Treeview(self.root, columns=cols, show="headings", height=height)
        for c in cols:
            t.heading(c, text=c); t.column(c, width=100, anchor="center")
        t.tag_configure("green", foreground="#2ecc71"); t.tag_configure("red", foreground="#ff5c5c")
        t.pack(fill="x", padx=10)
        return t

    def on_mode(self, _e=None):
        label = self.mode_var.get()
        mode = next(k for k, p in PROFILES.items() if p["label"] == label)
        err = self.bot.switch(mode=mode)
        if err:
            self.bot.log(err)
            self.mode_var.set(PROFILES[self.bot.mode]["label"])
        self.btn.config(text="START BOT", bg="#1f9d55")
        self.refresh()

    def on_tf(self, _e=None):
        self.bot.switch(tf=self.tf_var.get())
        self.refresh()

    def toggle(self):
        self.bot.toggle()
        self.refresh()

    def close_all(self):
        self.bot.close_all()
        self.refresh()

    def reset(self):
        self.bot.reset_account()
        self.refresh()

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.logbox.insert("end", data + "\n"); self.logbox.see("end")
                else:
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(500, self.poll)

    def refresh(self):
        b = self.bot
        with b.lock:
            cur = b.prof["currency"]
            eq = b.total_equity()
            pnl, today = eq - b.start_equity, eq - b.day_start_equity
            wins = sum(1 for t in b.trades if t["pnl"] > 0)
            wr = f"{wins / len(b.trades) * 100:.0f}%" if b.trades else "-"
            status = "PAUSED (daily limit)" if b.paused_today else ("RUNNING" if b.running else "STOPPED")
            market = list(b.market.items()); positions = list(b.positions.items())
            trades = list(b.trades[-30:]); session = b.session
        self.btn.config(text="STOP BOT" if b.running else "START BOT", bg="#c0392b" if b.running else "#1f9d55")
        self.stat["Equity"].config(text=f"{eq:,.2f}")
        self.stat["Total PnL"].config(text=f"{pnl:+,.2f}", fg="#2ecc71" if pnl >= 0 else "#ff5c5c")
        self.stat["Today PnL"].config(text=f"{today:+,.2f}", fg="#2ecc71" if today >= 0 else "#ff5c5c")
        self.stat["Win rate"].config(text=wr)
        self.stat["Trades"].config(text=str(len(b.trades)))
        self.stat["Max DD"].config(text=f"{b.max_dd:.2f}%")
        self.stat["Status"].config(text=status, fg="#f1c40f" if b.paused_today else ("#2ecc71" if b.running else "#8b93a7"))
        self.stat["Market"].config(text=session, fg="#2ecc71" if session in ("OPEN", "24x7") else "#ff5c5c")
        self.info.config(text=f"{cur} | TF {b.tf} | Risk {RISK_PER_TRADE*100:.0f}% | RR 1:{RR:g} | PAPER")

        for t in (self.mk, self.ps, self.hs):
            t.delete(*t.get_children())
        for s, m in market:
            tag = "green" if m["signal"].startswith("LONG") else ("red" if m["signal"].startswith("SHORT") else "")
            lo = f"{m['near_lo']:.2f}" if m["near_lo"] else "-"
            hi = f"{m['near_hi']:.2f}" if m["near_hi"] else "-"
            self.mk.insert("", "end", tags=(tag,), values=(
                m["label"], f"{m['price']:.2f}", lo, hi, m["accum"], f"{m['atr']:.2f}", m["signal"]))
        for s, p in positions:
            u = dict(market).get(s, {}).get("upnl", 0)
            self.ps.insert("", "end", tags=("green" if u >= 0 else "red",), values=(
                sym_label(s), p["side"], f"{p['entry']:.2f}", f"{p['sl']:.2f}", f"{p['tp']:.2f}",
                f"{p['qty']:g}", f"{u:+.2f}"))
        for tr in reversed(trades):
            self.hs.insert("", "end", tags=("green" if tr["pnl"] > 0 else "red",), values=(
                tr["time"], tr["symbol"], tr["side"], tr["entry"], tr["exit"],
                f"{tr['pnl']:+.2f}", tr["r"], tr["reason"]))


if __name__ == "__main__":
    if tk is None:
        raise SystemExit("tkinter not found - run algo_bot_web.py instead (phone/browser dashboard)")
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()
