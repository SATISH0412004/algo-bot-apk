"""
algo_bot_web.py  -  Phone / browser dashboard for the SAME paper-trading bot.

Keep algo_bot_dashboard.py in the same folder (this file imports the bot logic from it,
so the Berlin liquidity-sweep strategy, risk rules and paper account are identical).

RUN    : python algo_bot_web.py      (pick Crypto / Indian Stocks and 5m / 15m on the dashboard)
OPEN   : on the same device ->  http://localhost:8080
         from phone on same WiFi -> http://<computer-ip>:8080   (the IP is printed at start)
OPTION : set a PIN so nobody else on the network can control the bot:
         Windows:  set BOT_PIN=1234     Mac/Linux/Termux:  export BOT_PIN=1234
PHONE APP: open the page in Chrome -> menu -> "Add to Home screen" -> it opens like an app.
Needs only standard Python 3.8+ (no tkinter, no pip installs).
"""

import json
import os
import queue
import socket
import threading
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from urllib.parse import urlparse, parse_qs
from algo_bot_dashboard import PaperBot, PROFILES, TIMEFRAMES, RISK_PER_TRADE, RR

HOST, PORT = "0.0.0.0", int(os.environ.get("PORT") or os.environ.get("BOT_PORT", "8080"))   # PORT is set by hosts like Render
ACCESS_PIN = os.environ.get("BOT_PIN", "")

q = queue.Queue()
bot = PaperBot(q)
logs = deque(maxlen=60)


def drain_queue():
    while True:
        kind, data = q.get()
        if kind == "log":
            logs.append(data)


def snapshot():
    b = bot
    with b.lock:
        eq = b.total_equity()
        wins = sum(1 for t in b.trades if t["pnl"] > 0)
        status = "PAUSED" if b.paused_today else ("RUNNING" if b.running else "STOPPED")
        market = [dict(symbol=s, **m) for s, m in b.market.items()]
        positions = []
        for s, p in b.positions.items():
            m = b.market.get(s, {})
            positions.append(dict(symbol=m.get("label", s), side=p["side"], entry=p["entry"], sl=p["sl"],
                                  tp=p["tp"], qty=p["qty"], upnl=m.get("upnl", 0)))
        return dict(
            equity=eq, pnl=eq - b.start_equity, today=eq - b.day_start_equity,
            winrate=(wins / len(b.trades) * 100) if b.trades else None,
            trades=len(b.trades), maxdd=b.max_dd, status=status, running=b.running,
            mode=b.mode, tf=b.tf, session=b.session, currency=b.prof["currency"],
            modes=[dict(id=k, label=v["label"]) for k, v in PROFILES.items()], tfs=TIMEFRAMES,
            info=f"{b.prof['currency']} | risk {RISK_PER_TRADE*100:.0f}% | RR 1:{RR:g} | PAPER",
            market=market, positions=positions,
            history=list(reversed(b.trades[-30:])), logs=list(logs)[-25:])


def do_action(name, qs):
    """Returns an error message (empty string = ok)."""
    b = bot
    if name == "toggle":
        b.toggle()
    elif name == "closeall":
        b.close_all()
    elif name == "reset":
        b.reset_account()
    elif name == "set":
        return b.switch(mode=(qs.get("mode") or [None])[0], tf=(qs.get("tf") or [None])[0])
    return ""


MANIFEST = json.dumps({"name": "Algo Bot", "short_name": "Algo Bot", "start_url": "/",
                       "display": "standalone", "background_color": "#10131a",
                       "theme_color": "#10131a"})

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#10131a"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json"><title>Algo Bot</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#10131a;color:#e6e6e6;font:14px system-ui,sans-serif;
padding:10px;padding-bottom:40px}
h2{font-size:13px;margin:16px 4px 6px;color:#fff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.card{background:#181c26;border-radius:10px;padding:10px 12px}
.card .l{color:#8b93a7;font-size:11px}.card .v{font-size:18px;font-weight:700;margin-top:2px}
.g{color:#2ecc71}.r{color:#ff5c5c}.y{color:#f1c40f}.m{color:#8b93a7}
.btns{display:flex;gap:8px;margin:10px 0}
button{flex:1;border:0;border-radius:10px;padding:13px;font-size:14px;font-weight:700;color:#fff}
.go{background:#1f9d55}.stop{background:#c0392b}.sec{background:#444b5e}
.row{background:#181c26;border-radius:10px;padding:10px 12px;margin-bottom:6px}
.row .t{display:flex;justify-content:space-between;font-weight:700}
.row .s{color:#8b93a7;font-size:12px;margin-top:4px;line-height:1.5}
.log{background:#0b0d12;border-radius:10px;padding:8px;font:11px monospace;color:#9fb3c8;
max-height:170px;overflow:auto;white-space:pre-wrap}
.seg{display:flex;gap:6px;margin:0 0 8px}.seg button{padding:10px;background:#232938;font-weight:600}
.seg button.on{background:#2b6cdf}
.info{color:#8b93a7;font-size:11px;text-align:center;margin-top:4px}
</style></head><body>
<div class="seg" id="modes"></div><div class="seg" id="tfs"></div>
<div class="grid" id="stats"></div>
<div class="btns"><button id="tog" class="go">START BOT</button>
<button id="cl" class="sec">Close all</button><button id="rs" class="sec">Reset</button></div>
<div class="info" id="info"></div>
<h2>Market scan</h2><div id="mk"></div>
<h2>Open positions</h2><div id="ps"></div>
<h2>Trade history</h2><div id="hs"></div>
<h2>Log</h2><div class="log" id="lg"></div>
<script>
let pin=localStorage.getItem('pin')||'';
const f=(n,d=2)=>n==null?'-':Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const cls=n=>n>=0?'g':'r';
async function api(path,method){
  const r=await fetch(path,{method:method||'GET',headers:{'X-Pin':pin}});
  if(r.status==401){pin=prompt('Enter PIN')||'';localStorage.setItem('pin',pin);return null}
  return r.json();
}
function card(l,v,c){return `<div class="card"><div class="l">${l}</div><div class="v ${c||''}">${v}</div></div>`}
function render(s){
  document.getElementById('stats').innerHTML=
    card('Equity ('+s.currency+')',f(s.equity))+card('Total PnL',(s.pnl>=0?'+':'')+f(s.pnl),cls(s.pnl))+
    card('Today PnL',(s.today>=0?'+':'')+f(s.today),cls(s.today))+
    card('Win rate',s.winrate==null?'-':f(s.winrate,0)+'%')+
    card('Trades',s.trades)+card('Max DD',f(s.maxdd)+'%')+
    card('Status',s.status,s.status=='RUNNING'?'g':(s.status=='PAUSED'?'y':'m'))+card('Market',s.session,(s.session=='CLOSED')?'r':'g');
  document.getElementById('modes').innerHTML=s.modes.map(m=>`<button class="${m.id==s.mode?'on':''}" onclick="act('set?mode=${m.id}')">${m.label}</button>`).join('');
  document.getElementById('tfs').innerHTML=s.tfs.map(t=>`<button class="${t==s.tf?'on':''}" onclick="act('set?tf=${t}')">${t}</button>`).join('');
  const t=document.getElementById('tog');
  t.textContent=s.running?'STOP BOT':'START BOT';t.className=s.running?'stop':'go';
  document.getElementById('info').textContent=s.info;
  document.getElementById('mk').innerHTML=s.market.map(m=>{
    const c=m.signal.startsWith('LONG')?'g':(m.signal.startsWith('SHORT')?'r':'');
    return `<div class="row"><div class="t"><span>${m.label}</span><span>${f(m.price)}</span></div>
    <div class="s">Liq below ${f(m.near_lo)} &nbsp;|&nbsp; Liq above ${f(m.near_hi)}<br>
    Accumulation: ${m.accum} &nbsp;|&nbsp; ATR ${f(m.atr)}<br><span class="${c}">${m.signal}</span></div></div>`}).join('')||'<div class="m">Waiting for data...</div>';
  document.getElementById('ps').innerHTML=s.positions.map(p=>
    `<div class="row"><div class="t"><span>${p.symbol} <span class="${p.side=='LONG'?'g':'r'}">${p.side}</span></span>
    <span class="${cls(p.upnl)}">${p.upnl>=0?'+':''}${f(p.upnl)}</span></div>
    <div class="s">Entry ${f(p.entry)} | SL ${f(p.sl)} | TP ${f(p.tp)} | Qty ${Number(p.qty)}</div></div>`).join('')||'<div class="m">No open trades</div>';
  document.getElementById('hs').innerHTML=s.history.map(h=>
    `<div class="row"><div class="t"><span>${h.symbol} ${h.side}</span><span class="${cls(h.pnl)}">${h.pnl>=0?'+':''}${f(h.pnl)} (${h.r}R)</span></div>
    <div class="s">${h.time} | ${h.entry} to ${h.exit} | ${h.reason}</div></div>`).join('')||'<div class="m">No trades yet</div>';
  const lg=document.getElementById('lg');lg.textContent=s.logs.join('\n');lg.scrollTop=lg.scrollHeight;
}
async function tick(){try{const s=await api('/api/state');if(s)render(s)}catch(e){}}
async function act(a,msg){if(msg&&!confirm(msg))return;const r=await api('/api/'+a,'POST');if(r&&r.msg)alert(r.msg);tick()}
document.getElementById('tog').onclick=()=>act('toggle');
document.getElementById('cl').onclick=()=>act('closeall','Close all paper positions?');
document.getElementById('rs').onclick=()=>act('reset','Reset paper account to start balance?');
tick();setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return not ACCESS_PIN or self.headers.get("X-Pin", "") == ACCESS_PIN

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE, "text/html")
        elif path == "/manifest.json":
            self._send(200, MANIFEST, "application/manifest+json")
        elif path == "/api/state":
            if not self._authed():
                return self._send(401, '{"error":"pin"}')
            self._send(200, json.dumps(snapshot()))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._authed():
            return self._send(401, '{"error":"pin"}')
        u = urlparse(self.path)
        name = u.path.replace("/api/", "", 1)
        if name in ("toggle", "closeall", "reset", "set"):
            msg = do_action(name, parse_qs(u.query))
            self._send(200, json.dumps({"ok": not msg, "msg": msg}))
        else:
            self._send(404, '{"error":"not found"}')


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "your-computer-ip"


if __name__ == "__main__":
    threading.Thread(target=drain_queue, daemon=True).start()
    threading.Thread(target=bot.loop, daemon=True).start()
    print(f"Algo bot (PAPER) running.\n  This device : http://localhost:{PORT}\n"
          f"  Phone (same WiFi): http://{lan_ip()}:{PORT}")
    if not ACCESS_PIN:
        print("  Tip: set BOT_PIN=1234 so only you can control it.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
