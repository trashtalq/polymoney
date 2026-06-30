#!/usr/bin/env python3
"""
copy_dashboard.py — ВЕБ-ДАШБОРД для бумажного копи (надстройка над copy_trader.py).

Сам крутит опрос целей в фоне и отдаёт живую страницу в браузере:
таблица по кошелькам, общий PnL, открытые позиции, лог сделок, кривая PnL.
Страница обновляется сама. Командная строка больше не нужна — это замена --watch.

Запуск:
    pip install flask          (один раз)
    python copy_dashboard.py --wallets addr1,addr2,... --bankroll 100000 --interval 120
    -> открой http://localhost:5000

Состояние то же (paper_book.json), что у copy_trader.py. НЕ запускай дашборд и
--watch одновременно на одном файле состояния — будут конфликтовать за запись.
"""

import argparse
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, Response

import copy_trader as ct

app = Flask(__name__)

# Пароль на изменяющие действия (удаление кошелька). В репо хранится только ХЭШ, не сам пароль.
# Можно переопределить на сервере через env ADMIN_PASS. По умолчанию — заданный пользователем.
ADMIN_HASH = (hashlib.sha256(os.environ["ADMIN_PASS"].encode("utf-8")).hexdigest()
              if os.environ.get("ADMIN_PASS")
              else "a7a73bb77842473cec098b5635043d41654b66dff80475a9f6a6178b6b36ea34")


_lock = threading.Lock()
STATE = {
    "book": None,
    "marks": {},
    "status": {"started_at": int(time.time()), "last_poll": 0, "polling": False,
               "error": "", "n_polls": 0, "wallets": 0},
    "cfg": {},
}


# ----------------------------- источник кошелька (какой скан нашёл) -----------------------------
_SRC = {"mtime": 0, "map": {}}


def get_sources() -> dict:
    """Карта wallet->метод поиска из wallet_sources.json, перечитывается при изменении файла."""
    p = Path("wallet_sources.json")
    try:
        m = p.stat().st_mtime
        if m != _SRC["mtime"]:
            _SRC["map"] = {k.lower(): v for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
            _SRC["mtime"] = m
    except Exception:  # noqa: BLE001
        pass
    return _SRC["map"]


# ----------------------------- текущий watchlist (для скрытия удалённых) -----------------------------
_WL = {"mtime": 0, "set": set(), "path": "copy_watchlist.json"}


def get_watchlist() -> set:
    p = Path(STATE.get("wl_path") or _WL["path"])
    try:
        m = p.stat().st_mtime
        if m != _WL["mtime"]:
            d = json.loads(p.read_text(encoding="utf-8"))
            _WL["set"] = {w.lower() for w in d.get("watchlist", [])}
            _WL["mtime"] = m
    except Exception:  # noqa: BLE001
        pass
    return _WL["set"]


# ----------------------------- статистика для страницы -----------------------------
def compute_stats(book: dict, marks: dict) -> dict:
    from collections import defaultdict

    def mark_val(p):
        mk = marks.get(p["token"])
        return p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]

    open_val = sum(mark_val(p) for p in book["positions"].values())
    invested_open = sum(p["cost"] for p in book["positions"].values())
    unrealized = open_val - invested_open
    realized = book["realized"]
    total = book["cash"] + open_val
    pnl = total - book["bankroll"]
    roi = pnl / book["bankroll"] if book["bankroll"] else 0.0

    stat = defaultdict(lambda: {"copied": 0, "closed": 0, "wins": 0, "realized": 0.0,
                                "open_val": 0.0, "open_cost": 0.0, "spent": 0.0, "open_n": 0})
    for r in book["log"]:
        w = r.get("w", "?")
        if r.get("act") == "BUY":
            stat[w]["copied"] += 1
            stat[w]["spent"] += r.get("spend", 0) or 0   # суммарно задействовано $ на эту цель
        if "pnl" in r:
            stat[w]["closed"] += 1
            stat[w]["realized"] += r["pnl"]
            stat[w]["wins"] += 1 if r["pnl"] > 0 else 0
    for p in book["positions"].values():
        w = p.get("wallet", "?")
        stat[w]["open_val"] += mark_val(p)
        stat[w]["open_cost"] += p["cost"]
        stat[w]["open_n"] += 1                        # число открытых позиций (усреднения уже слиты)

    sources = get_sources()
    MIN_CLOSED_FOR_VERDICT = 8                        # вердикт только после N ЗАКРЫТЫХ сделок
    per_wallet = []
    for w, s in stat.items():
        unreal = s["open_val"] - s["open_cost"]
        total = s["realized"] + unreal                # форвардный PnL цели в $ (реализ.+нереализ.)
        # надёжный вердикт: судим по РЕАЛИЗОВАННОМУ (нереализ. шумит у hold-to-resolution),
        # и только когда закрыто достаточно сделок. Нереализ. — лишь слабый довесок.
        score = s["realized"] + 0.3 * unreal
        if s["closed"] >= MIN_CLOSED_FOR_VERDICT:
            flag = "lead" if score > 0 else "drop"
        else:
            flag = ""                                 # мало ЗАКРЫТЫХ — рано судить
        per_wallet.append({
            "wallet": w,
            "copied": s["copied"],
            "closed": s["closed"],
            "win_rate": (s["wins"] / s["closed"]) if s["closed"] else 0.0,
            "open_n": s["open_n"],
            "realized": round(s["realized"], 2),
            "unrealized": round(unreal, 2),
            "total": round(total, 2),
            "open_val": round(s["open_val"], 2),
            "spent": round(s["spent"], 2),
            "flag": flag,
            "source": sources.get(w, "—"),
        })
    wlset = get_watchlist()
    if wlset:                                                  # скрываем удалённые из рейтинга (позиции их доживут сами)
        per_wallet = [w for w in per_wallet if w["wallet"].lower() in wlset]
    per_wallet.sort(key=lambda x: x["total"], reverse=True)   # авто-ранжирование по форвардному PnL

    positions = sorted(book["positions"].values(), key=lambda p: p["cost"], reverse=True)
    open_list = []
    for p in positions:                              # ВСЕ открытые позиции (без обрезки)
        v = mark_val(p)
        entry = (p["cost"] / p["qty"]) if p.get("qty") else None
        open_list.append({
            "wallet": p.get("wallet", ""),
            "title": p.get("title", ""),
            "outcome": p.get("outcome", ""),         # ДА/НЕТ — какую сторону взяли
            "entry": round(entry, 4) if entry else None,   # наша средняя цена входа
            "mark": marks.get(p["token"]),           # текущий реальный кэф рынка
            "cost": round(p["cost"], 2),
            "val": round(v, 2),
            "pnl": round(v - p["cost"], 2),
            "roi": round((v - p["cost"]) / p["cost"], 4) if p["cost"] else 0.0,
        })

    log = []
    for r in book["log"][-60:][::-1]:
        log.append({
            "t": r.get("t", 0),
            "w": r.get("w", ""),
            "act": r.get("act", ""),
            "pnl": r.get("pnl"),
            "spend": r.get("spend"),
            "out": r.get("out", ""),
            "title": r.get("title", ""),
        })

    return {
        "bankroll": book["bankroll"],
        "cash": round(book["cash"], 2),
        "realized": round(realized, 2),
        "unrealized": round(unrealized, 2),
        "invested_open": round(invested_open, 2),
        "open_val": round(open_val, 2),
        "total": round(total, 2),
        "pnl": round(pnl, 2),
        "roi": roi,
        "n_copied": book["n_copied"],
        "n_skipped": book.get("n_skipped", 0),
        "n_open": len(book["positions"]),
        "topups": round(book.get("topups", 0.0), 2),
        "started": book["started"],
        "per_wallet": per_wallet,
        "open_positions": open_list,
        "log": log,
        "pnl_history": book.get("pnl_history", []),
    }


# ----------------------------- фоновый опрос -----------------------------
def _reload_wallets(wl_path, fallback):
    """Горячая перезагрузка watchlist: правки файла подхватываются без рестарта."""
    if not wl_path:
        return fallback
    try:
        d = json.loads(Path(wl_path).read_text(encoding="utf-8"))
        wl = [w.lower() for w in d.get("watchlist", [])]
        return wl or fallback
    except Exception:  # noqa: BLE001
        return fallback


def poll_loop(api, wallets, per_trade, slippage, interval, state_file, wl_path=None):
    while True:
        try:
            wallets = _reload_wallets(wl_path, wallets)     # перечитываем список целей каждый цикл
            with _lock:
                STATE["status"]["polling"] = True
                STATE["status"]["wallets"] = len(wallets)
                working = copy.deepcopy(STATE["book"])
            # сам цикл (API-запросы) — БЕЗ блокировки, чтобы страница не висла
            marks = ct.cycle(api, working, wallets, per_trade, slippage)
            # точка для кривой PnL
            ov = 0.0
            for p in working["positions"].values():
                mk = marks.get(p["token"])
                ov += p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]
            total_pnl = round(working["cash"] + ov - working["bankroll"], 2)
            hist = working.setdefault("pnl_history", [])
            hist.append([int(time.time()), round(working["realized"], 2), total_pnl])
            if len(hist) > 2000:
                del hist[:len(hist) - 2000]
            with _lock:
                STATE["book"] = working
                STATE["marks"] = marks
                STATE["status"]["last_poll"] = int(time.time())
                STATE["status"]["n_polls"] += 1
                STATE["status"]["polling"] = False
                STATE["status"]["error"] = ""
                ct.save_book(state_file, working)
        except Exception as e:  # noqa: BLE001
            with _lock:
                STATE["status"]["error"] = str(e)
                STATE["status"]["polling"] = False
            print(f"[poll error] {e}", flush=True)
        time.sleep(interval)


# ----------------------------- страница -----------------------------
PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copy — живой PnL</title>
<style>
  :root{
    --bg:#05080d; --panel:#0a1019; --panel2:#0e1622; --border:#15323d;
    --text:#cdeef0; --muted:#5d7c8a; --green:#2bf5b0; --red:#ff3b6b;
    --accent:#06e5ff; --amber:#ffc23d;
  }
  *{box-sizing:border-box}
  body{margin:0;color:var(--text);font-size:14px;
       font-family:ui-monospace,"JetBrains Mono",SFMono-Regular,Menlo,Consolas,monospace;
       background:
         radial-gradient(1200px 600px at 82% -12%, rgba(6,229,255,.07), transparent 60%),
         radial-gradient(900px 520px at -5% 112%, rgba(255,59,107,.06), transparent 60%),
         linear-gradient(180deg,#05080d,#04060a);
       background-attachment:fixed}
  body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
       background:repeating-linear-gradient(0deg,rgba(0,0,0,0) 0 2px,rgba(0,0,0,.16) 2px 3px);opacity:.4}
  .num{font-variant-numeric:tabular-nums}
  .wrap{max-width:1100px;margin:0 auto;padding:22px 18px 60px;position:relative;z-index:1}
  header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:6px}
  h1{font-size:16px;font-weight:700;letter-spacing:1.5px;margin:0;text-transform:uppercase;
     color:var(--accent);text-shadow:0 0 10px rgba(6,229,255,.55)}
  h1 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);margin-right:8px;vertical-align:middle}
  h1 .dot.live{background:var(--green);box-shadow:0 0 10px var(--green),0 0 22px var(--green);animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .status{color:var(--muted);font-size:12px}
  .status b{color:var(--accent);font-weight:700}
  .hero{font-size:42px;font-weight:700;margin:10px 0 2px;letter-spacing:-.5px;text-shadow:0 0 20px currentColor}
  .hero .sub{font-size:13px;color:var(--muted);margin-left:10px;font-weight:400;text-shadow:none}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--border);
        border-radius:8px;padding:13px 15px;position:relative;overflow:hidden}
  .card::before{content:"";position:absolute;left:0;top:0;height:100%;width:2px;background:var(--accent);
        opacity:.7;box-shadow:0 0 10px var(--accent)}
  .card .k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px}
  .card .v{font-size:22px;font-weight:700;margin-top:5px}
  .sec{margin:26px 0 10px;font-size:12px;color:var(--accent);text-transform:uppercase;letter-spacing:2px;
       border-left:3px solid var(--accent);padding-left:9px;text-shadow:0 0 8px rgba(6,229,255,.4)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
  th,td{padding:10px 13px;text-align:right;border-bottom:1px solid var(--border)}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--accent);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:700;
     background:var(--panel2);border-bottom:1px solid var(--accent)}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:rgba(6,229,255,.06)}
  .pos{color:var(--green);text-shadow:0 0 7px rgba(43,245,176,.45)}
  .neg{color:var(--red);text-shadow:0 0 7px rgba(255,59,107,.45)} .zero{color:var(--muted)}
  .addr{font-family:ui-monospace,monospace;color:var(--accent);font-size:12px}
  .addr.clk{cursor:pointer;text-decoration:underline dotted}
  .addr.clk:hover{color:#fff}
  .del{cursor:pointer;color:var(--muted);font-weight:700;padding:0 6px;border-radius:5px}
  .del:hover{color:#fff;background:rgba(255,59,107,.25)}
  .title{color:var(--text);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
  .tag.BUY{background:rgba(88,166,255,.15);color:var(--accent)}
  .tag.SELL{background:rgba(210,153,34,.15);color:var(--amber)}
  .tag.REDEEM,.tag.SETTLE{background:rgba(63,185,80,.15);color:var(--green)}
  .tag.YES{background:rgba(63,185,80,.15);color:var(--green)}
  .tag.NO{background:rgba(248,81,73,.15);color:var(--red)}
  .empty{color:var(--muted);padding:18px 4px;font-style:italic}
  .spark{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px}
  .muted{color:var(--muted)} .err{color:var(--red);font-size:12px;margin-top:6px}
  .mono{font-family:ui-monospace,monospace}
  .card.clickable{cursor:pointer;transition:border-color .15s,transform .05s}
  .card.clickable:hover{border-color:var(--accent)}
  .card .hint{color:var(--accent);font-size:10px;margin-top:6px;letter-spacing:.3px}
  .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;padding:30px 16px;overflow:auto}
  .modal-bg.show{display:block}
  .modal{max-width:1000px;margin:0 auto;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
  .modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
  .modal-head b{font-size:15px}
  .x{cursor:pointer;color:var(--muted);font-size:18px;padding:0 6px}
  .x:hover{color:var(--text)}
  .skipsum{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:12px 0 16px}
  .skipsum .b{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:9px 11px}
  .skipsum .b .k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.4px}
  .skipsum .b .v{font-family:ui-monospace,monospace;font-size:18px;font-weight:600;margin-top:3px}
  .rtag{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;font-weight:600;background:var(--panel2);color:var(--muted)}
  .st-win{color:var(--green)} .st-lose{color:var(--red)} .st-open{color:var(--muted)}
  .gem{background:rgba(63,185,80,.10)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span id="dot" class="dot"></span>Бумажное копи — живой PnL</h1>
    <div class="status" id="status">подключение…</div>
  </header>

  <div class="hero num" id="hero">—<span class="sub" id="herosub"></span></div>
  <div class="err" id="err"></div>

  <div class="cards" id="cards"></div>

  <div class="sec">Кривая PnL</div>
  <div class="spark"><svg id="spark" width="100%" height="90" preserveAspectRatio="none"></svg></div>

  <div class="sec">По кошелькам — авто-ранжирование по форвардному PnL</div>
  <table>
    <thead><tr><th>#</th><th>Кошелёк</th><th>Источник</th><th>Скопир.</th><th>Задейств.</th><th>Закрыто</th><th>Открыто</th><th>Винрейт</th><th>Реализ. PnL</th><th>Нереализ. PnL</th><th>PnL итого</th><th>Статус</th><th></th></tr></thead>
    <tbody id="wallets"></tbody>
  </table>

  <div class="sec">Открытые позиции — все</div>
  <table>
    <thead><tr><th>Кошелёк</th><th>Рынок</th><th>Ставка</th><th>Вход</th><th>Тек. кэф</th><th>Вложено</th><th>Оценка</th><th>P/L</th></tr></thead>
    <tbody id="open"></tbody>
  </table>

  <div class="sec">Последние действия</div>
  <table>
    <thead><tr><th>Время</th><th>Кошелёк</th><th>Действие</th><th>Ставка</th><th>Рынок</th><th>PnL / сумма</th></tr></thead>
    <tbody id="log"></tbody>
  </table>
</div>

<div class="modal-bg" id="modal" onclick="if(event.target===this)closeSkipped()">
  <div class="modal">
    <div class="modal-head">
      <b>Отфильтрованные сделки — теневой бэктест фильтра</b>
      <span class="x" onclick="closeSkipped()">✕</span>
    </div>
    <div class="muted" style="font-size:12px">Что было бы, если бы фильтр НЕ резал эти входы (нотионал $10 на вход). Плюс = фильтр стоил нам денег → стоит ослабить.</div>
    <div class="skipsum" id="skipsum"></div>
    <div class="sec" style="margin:18px 0 8px">По кошелькам — кто плюсует/минусует на отфильтрованных</div>
    <table>
      <thead><tr><th>Кошелёк</th><th>Источник</th><th>Отфильтр.</th><th>Резолв</th><th>Винрейт</th><th>Реализ.</th><th>Открыт.</th><th>Итого</th></tr></thead>
      <tbody id="skipwallets"></tbody>
    </table>
    <div class="sec" style="margin:18px 0 8px">Сделки</div>
    <table>
      <thead><tr><th>Время</th><th>Кошелёк</th><th>Причина</th><th>Рынок</th><th>Вход</th><th>Цена цели</th><th>Тек/исход</th><th>Статус</th><th>Теневой PnL</th></tr></thead>
      <tbody id="skiprows"></tbody>
    </table>
  </div>
</div>

<div class="modal-bg" id="wmodal" onclick="if(event.target===this)closeWallet()">
  <div class="modal">
    <div class="modal-head">
      <b id="wtitle">Кошелёк</b>
      <span class="x" onclick="closeWallet()">✕</span>
    </div>
    <div class="sec" style="margin:12px 0 8px">Сводка — реально скопировано</div>
    <div class="skipsum" id="wsum"></div>
    <div class="sec" style="margin:16px 0 8px">Теневые (отсеяны фильтром EV, нотионал $10)</div>
    <div class="skipsum" id="wshadow"></div>
    <table style="margin-top:10px">
      <thead><tr><th>Рынок</th><th>Ставка</th><th>Причина</th><th>Вход</th><th>Статус</th><th>Теневой PnL</th></tr></thead>
      <tbody id="wshadowrows"></tbody>
    </table>
    <div class="sec" style="margin:16px 0 8px">🟢 Открытые позиции <span id="wopenc" class="muted"></span></div>
    <table>
      <thead><tr><th>Рынок</th><th>Ставка</th><th>Открыта</th><th>Вход</th><th>Тек. кэф</th><th>Вложено</th><th>Оценка</th><th>P/L</th></tr></thead>
      <tbody id="wpos"></tbody>
    </table>
    <div class="sec" style="margin:16px 0 8px">⚪ Закрытые позиции <span id="wclosedc" class="muted"></span></div>
    <table>
      <thead><tr><th>Рынок</th><th>Ставка</th><th>Покупок</th><th>Вложено</th><th>Реализ. P/L</th><th>Результат</th></tr></thead>
      <tbody id="wclosed"></tbody>
    </table>
    <div class="sec" style="margin:16px 0 8px">Сделки — хронология</div>
    <table>
      <thead><tr><th>Время</th><th>Тип</th><th>Действие</th><th>Ставка</th><th>Рынок</th><th>PnL / сумма</th></tr></thead>
      <tbody id="wlog"></tbody>
    </table>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const money = v => (v<0?"-":"")+"$"+Math.abs(v).toLocaleString("en-US",{maximumFractionDigits:0});
const cls = v => v>0.005?"pos":(v<-0.005?"neg":"zero");
const ago = ts => { if(!ts) return "—"; const s=Math.floor(Date.now()/1000-ts);
  if(s<60) return s+"с назад"; if(s<3600) return Math.floor(s/60)+"м назад";
  if(s<86400) return Math.floor(s/3600)+"ч назад"; return Math.floor(s/86400)+"д назад"; };
const shortAddr = a => a ? a.slice(0,8)+"…"+a.slice(-4) : "?";
const sideBadge = o => {
  const s=(o||"").toString().toLowerCase();
  if(s==="yes"||s==="да")  return '<span class="tag YES">ДА</span>';
  if(s==="no"||s==="нет")  return '<span class="tag NO">НЕТ</span>';
  return o ? '<span class="rtag">'+o+'</span>' : '<span class="muted">—</span>';
};
const flagBadge = f => f==="lead" ? '<span class="tag YES">лидер</span>'
  : f==="drop" ? '<span class="tag NO">на отсев</span>' : '<span class="muted">—</span>';
const addrLink = a => '<span class="addr clk" onclick="openWallet(\''+a+'\')">'+shortAddr(a)+'</span>';
async function removeWallet(a){
  if(!confirm("Удалить кошелёк "+shortAddr(a)+"?\nКопирование новых сделок прекратится. Открытые позиции до-резолвятся сами.")) return;
  let pw = localStorage.getItem("pw") || "";
  if(!pw){ pw = prompt("Пароль:") || ""; if(!pw) return; localStorage.setItem("pw", pw); }
  try{
    const resp = await fetch("/api/remove_wallet",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({wallet:a, pw:pw})});
    if(resp.status===401){ localStorage.removeItem("pw"); alert("Неверный пароль — попробуй ещё раз"); return; }
    const r = await resp.json();
    if(!r.ok){ alert("Не удалось удалить: "+(r.error||"")); return; }
  }catch(e){ alert("Ошибка сети"); return; }
  tick();
}
let curWallet=null;
function closeWallet(){ curWallet=null; $("wmodal").classList.remove("show"); }
async function openWallet(a){ curWallet=a; $("wmodal").classList.add("show"); await loadWallet(); }
async function loadWallet(){
  if(!curWallet) return;
  let d; try{ d=await (await fetch("/api/wallet?addr="+curWallet)).json(); }catch(e){ return; }
  $("wtitle").innerHTML = shortAddr(d.wallet)+' &nbsp;<span class="rtag">'+(d.source||"—")+'</span> '+
    '&nbsp;<a class="addr" href="https://polymarket.com/profile/'+d.wallet+'" target="_blank">профиль ↗</a>';
  $("wsum").innerHTML = [
    ["Скопировано BUY", d.n_buys, ""],
    ["Задействовано", money(d.spent), ""],
    ["Открытых", d.n_open, ""],
    ["Реализ. PnL", money(d.realized), cls(d.realized)],
    ["Нереализ. PnL", money(d.unrealized), cls(d.unrealized)],
    ["PnL / задейств.", (d.spent>0?(((d.realized+d.unrealized)/d.spent>=0?'+':'')+((d.realized+d.unrealized)/d.spent*100).toFixed(0)+'%'):'—'), cls(d.realized+d.unrealized)],
  ].map(b=>'<div class="b"><div class="k">'+b[0]+'</div><div class="v '+b[2]+'">'+b[1]+'</div></div>').join("");
  const sh=d.shadow||{};
  const swr = sh.resolved ? Math.round(100*sh.wins/sh.resolved)+"%" : "—";
  $("wshadow").innerHTML = [
    ["Отфильтровано", sh.n||0, ""],
    ["Резолв", (sh.resolved||0), ""],
    ["Винрейт", swr, ""],
    ["Теневой реализ.", money(sh.real||0), cls(sh.real||0)],
    ["Теневой итого", money(sh.total||0), cls(sh.total||0)],
  ].map(b=>'<div class="b"><div class="k">'+b[0]+'</div><div class="v '+b[2]+'">'+b[1]+'</div></div>').join("");
  $("wshadowrows").innerHTML = (d.shadow_rows||[]).map(r=>{
    const sp = r.shadow_pnl;
    const spc = sp==null ? '<span class="muted">—</span>' : '<span class="num '+cls(sp)+'">'+money(sp)+'</span>';
    const stl = {win:'<span class="tag YES">выигрыш</span>',lose:'<span class="tag NO">проигрыш</span>',open:'<span class="muted">открыта</span>'}[r.status]||r.status;
    return '<tr><td class="title">'+(r.title||"")+'</td>'+
      '<td>'+sideBadge(r.out)+'</td>'+
      '<td><span class="rtag">'+(REASON_LBL[r.reason]||r.reason)+'</span></td>'+
      '<td class="num muted">'+(r.entry!=null?r.entry.toFixed(3):"—")+'</td>'+
      '<td>'+stl+'</td><td>'+spc+'</td></tr>';
  }).join("") || '<tr><td colspan="6" class="empty">теневых сделок нет</td></tr>';
  $("wopenc").textContent = "("+(d.n_open||0)+")";
  $("wclosedc").textContent = "("+(d.n_closed||0)+")";
  $("wpos").innerHTML = (d.positions||[]).map(p=>{
    const pl=p.pnl;
    return '<tr><td class="title">'+(p.title||"")+(p.fills>1?' <span class="muted">(×'+p.fills+')</span>':'')+'</td>'+
      '<td>'+sideBadge(p.outcome)+'</td>'+
      '<td class="muted" style="font-size:12px">'+ago(p.opened)+'</td>'+
      '<td class="num muted">'+(p.entry!=null?p.entry.toFixed(3):"—")+'</td>'+
      '<td class="num">'+(p.mark!=null?p.mark.toFixed(3):"—")+'</td>'+
      '<td class="num muted">'+money(p.cost)+'</td>'+
      '<td class="num '+cls(pl)+'">'+money(p.val)+'</td>'+
      '<td class="num '+cls(pl)+'">'+money(pl)+'</td></tr>';
  }).join("") || '<tr><td colspan="8" class="empty">открытых позиций нет</td></tr>';
  $("wclosed").innerHTML = (d.closed||[]).map(c=>{
    const res = c.result==="win" ? '<span class="tag YES">выигрыш</span>'
              : c.result==="lose" ? '<span class="tag NO">проигрыш</span>' : '<span class="muted">0</span>';
    return '<tr><td class="title">'+(c.title||"")+(c.buys>1?' <span class="muted">(×'+c.buys+')</span>':'')+'</td>'+
      '<td>'+sideBadge(c.out)+'</td>'+
      '<td class="num muted">'+c.buys+'</td>'+
      '<td class="num muted">'+money(c.invested)+'</td>'+
      '<td class="num '+cls(c.realized)+'">'+money(c.realized)+'</td>'+
      '<td>'+res+'</td></tr>';
  }).join("") || '<tr><td colspan="6" class="empty">закрытых позиций пока нет</td></tr>';
  $("wlog").innerHTML = (d.log||[]).map(r=>{
    const v = r.pnl!=null ? '<span class="num '+cls(r.pnl)+'">'+money(r.pnl)+'</span>'
                          : '<span class="num muted">'+money(r.spend||0)+'</span>';
    const kind = r.kind==="close" ? '<span class="rtag" style="color:var(--amber)">выход</span>'
                                   : '<span class="rtag" style="color:var(--accent)">вход</span>';
    return '<tr><td class="mono muted">'+tm(r.t)+'</td>'+
      '<td>'+kind+'</td>'+
      '<td><span class="tag '+r.act+'">'+r.act+'</span></td>'+
      '<td>'+sideBadge(r.out)+'</td>'+
      '<td class="title">'+(r.title||"")+'</td><td>'+v+'</td></tr>';
  }).join("") || '<tr><td colspan="6" class="empty">сделок нет</td></tr>';
}
const tm = ts => { const d=new Date(ts*1000);
  return String(d.getMonth()+1).padStart(2,'0')+"-"+String(d.getDate()).padStart(2,'0')+" "+
         String(d.getHours()).padStart(2,'0')+":"+String(d.getMinutes()).padStart(2,'0'); };

function sparkline(hist){
  const svg=$("spark"); const W=svg.clientWidth||1000, H=90, pad=6;
  svg.innerHTML="";
  if(!hist||hist.length<2){ svg.innerHTML='<text x="10" y="48" fill="#8b949e" font-size="12">данных пока нет</text>'; return; }
  const xs=hist.map(p=>p[0]), ys=hist.map(p=>p[2]); // total pnl
  const x0=Math.min(...xs), x1=Math.max(...xs); let y0=Math.min(...ys,0), y1=Math.max(...ys,0);
  if(y1===y0){y1+=1;y0-=1;}
  const X=t=>pad+(W-2*pad)*(x1===x0?0.5:(t-x0)/(x1-x0));
  const Y=v=>pad+(H-2*pad)*(1-(v-y0)/(y1-y0));
  // нулевая линия
  const yz=Y(0);
  let g='<line x1="'+pad+'" y1="'+yz+'" x2="'+(W-pad)+'" y2="'+yz+'" stroke="#30363d" stroke-dasharray="3 3"/>';
  const last=ys[ys.length-1];
  const col=last>0?"#3fb950":(last<0?"#f85149":"#8b949e");
  let d="M "+X(xs[0])+" "+Y(ys[0]);
  for(let i=1;i<hist.length;i++) d+=" L "+X(xs[i])+" "+Y(ys[i]);
  // заливка
  let area=d+" L "+X(xs[xs.length-1])+" "+yz+" L "+X(xs[0])+" "+yz+" Z";
  g+='<path d="'+area+'" fill="'+col+'" opacity="0.10"/>';
  g+='<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="2"/>';
  svg.innerHTML=g;
}

async function tick(){
  let d;
  try{ d=await (await fetch("/api/state")).json(); }
  catch(e){ $("status").innerHTML="сервер недоступен"; return; }
  const st=d.status||{};
  $("dot").className="dot"+(st.polling?" live":(st.last_poll?" live":""));
  $("status").innerHTML='целей <b>'+(st.wallets||0)+'</b> · опрос <b>'+ago(st.last_poll)+'</b> · циклов <b>'+(st.n_polls||0)+'</b>'+(st.polling?' · <span class="muted">опрашиваю…</span>':'');
  $("err").textContent = st.error ? ("ошибка опроса: "+st.error) : "";

  $("hero").className="hero num "+cls(d.pnl);
  $("hero").innerHTML = money(d.pnl)+'<span class="sub">итого PnL · реализ. '+money(d.realized)+' · '+(d.roi*100).toFixed(1)+'% от банкролла</span>';

  $("cards").innerHTML = [
    ["Реализовано (закрытые)", money(d.realized), cls(d.realized)],
    ["Нереализ. PnL (открытые)", money(d.unrealized), cls(d.unrealized)],
    ["Открытых позиций", d.n_open, ""],
    ["BUY-действий (с усредн.)", d.n_copied, ""],
    ["Вложено в открытые", money(d.invested_open), ""],
    ["Отфильтровано (защита EV)", d.n_skipped||0, "", "skip"],
    ["Свободный кэш", money(d.cash), ""],
    ["Долито капитала", money(d.topups||0), ""],
    ["Банкролл (с доливами)", money(d.bankroll), ""],
  ].map(c=>'<div class="card'+(c[3]?' clickable" onclick="openSkipped()"':'"')+'><div class="k">'+c[0]+'</div><div class="v '+c[2]+'">'+c[1]+'</div>'+(c[3]?'<div class="hint">открыть журнал ›</div>':'')+'</div>').join("");

  sparkline(d.pnl_history);

  $("wallets").innerHTML = (d.per_wallet||[]).map((w,i)=>
    '<tr><td class="num muted">'+(i+1)+'</td>'+
    '<td>'+addrLink(w.wallet)+'</td>'+
    '<td><span class="rtag">'+(w.source||"—")+'</span></td>'+
    '<td class="num">'+w.copied+'</td>'+
    '<td class="num muted">'+money(w.spent)+'</td>'+
    '<td class="num">'+w.closed+'</td>'+
    '<td class="num">'+(w.open_n||0)+'</td>'+
    '<td class="num">'+(w.closed?(w.win_rate*100).toFixed(0)+"%":"—")+'</td>'+
    '<td class="num '+cls(w.realized)+'">'+money(w.realized)+'</td>'+
    '<td class="num '+cls(w.unrealized)+'">'+money(w.unrealized)+'</td>'+
    '<td class="num '+cls(w.total)+'">'+money(w.total)+(w.spent>0?' <span class="muted">('+(w.total/w.spent>=0?'+':'')+(w.total/w.spent*100).toFixed(0)+'%)</span>':'')+'</td>'+
    '<td>'+flagBadge(w.flag)+'</td>'+
    '<td><span class="del" title="удалить кошелёк" onclick="removeWallet(\''+w.wallet+'\')">✕</span></td></tr>').join("")
    || '<tr><td colspan="13" class="empty">пока ничего не скопировано — ждём первые сделки целей</td></tr>';

  $("open").innerHTML = (d.open_positions||[]).map(p=>{
    const pl = p.pnl!=null ? p.pnl : (p.val-p.cost);
    const roi = p.roi!=null ? (' <span class="muted">'+(p.roi*100).toFixed(0)+'%</span>') : "";
    return '<tr><td>'+addrLink(p.wallet)+'</td>'+
    '<td class="title">'+(p.title||"")+'</td>'+
    '<td>'+sideBadge(p.outcome)+'</td>'+
    '<td class="num muted">'+(p.entry!=null?p.entry.toFixed(3):"—")+'</td>'+
    '<td class="num">'+(p.mark!=null?p.mark.toFixed(3):"—")+'</td>'+
    '<td class="num muted">'+money(p.cost)+'</td>'+
    '<td class="num '+cls(pl)+'">'+money(p.val)+'</td>'+
    '<td class="num '+cls(pl)+'">'+money(pl)+roi+'</td></tr>';}).join("")
    || '<tr><td colspan="8" class="empty">открытых позиций нет</td></tr>';

  $("log").innerHTML = (d.log||[]).map(r=>{
    const v = r.pnl!=null ? '<span class="num '+cls(r.pnl)+'">'+money(r.pnl)+'</span>'
                          : '<span class="num muted">'+money(r.spend||0)+'</span>';
    return '<tr><td class="mono muted">'+tm(r.t)+'</td>'+
    '<td>'+addrLink(r.w)+'</td>'+
    '<td><span class="tag '+r.act+'">'+r.act+'</span></td>'+
    '<td>'+sideBadge(r.out)+'</td>'+
    '<td class="title">'+(r.title||"")+'</td><td>'+v+'</td></tr>';}).join("")
    || '<tr><td colspan="6" class="empty">журнал пуст</td></tr>';
}
const REASON_LBL = {band:"цена у края", adverse:"догон от цели", avg_up:"догон вверх", cap:"потолок позиции"};
let skipOpen=false;
function closeSkipped(){ skipOpen=false; $("modal").classList.remove("show"); }
async function openSkipped(){
  skipOpen=true; $("modal").classList.add("show");
  await loadSkipped();
}
async function loadSkipped(){
  let d;
  try{ d=await (await fetch("/api/skipped")).json(); }
  catch(e){ $("skiprows").innerHTML='<tr><td colspan="9" class="empty">не удалось загрузить</td></tr>'; return; }
  const reasons = Object.entries(d.by_reason||{}).map(([k,v])=>(REASON_LBL[k]||k)+": "+v).join(" · ") || "—";
  const wr = d.resolved_n ? Math.round(100*d.wins/d.resolved_n)+"%" : "—";
  $("skipsum").innerHTML = [
    ["Теневой реализ. PnL", money(d.realized), cls(d.realized)],
    ["Открытый теневой PnL", money(d.open_pnl), cls(d.open_pnl)],
    ["Зарезолвилось", d.resolved_n+" / "+d.count, ""],
    ["Винрейт отсеянных", wr, ""],
  ].map(b=>'<div class="b"><div class="k">'+b[0]+'</div><div class="v '+b[2]+'">'+b[1]+'</div></div>').join("")
   + '<div class="b" style="grid-column:1/-1"><div class="k">по причинам</div><div class="v" style="font-size:12px;font-family:system-ui">'+reasons+'</div></div>';
  $("skipwallets").innerHTML = (d.per_wallet||[]).map(w=>{
    const wr = w.resolved ? Math.round(100*w.wins/w.resolved)+"%" : "—";
    return '<tr><td>'+addrLink(w.wallet)+'</td>'+
      '<td><span class="rtag">'+(w.source||"—")+'</span></td>'+
      '<td class="num">'+w.n+'</td><td class="num">'+w.resolved+'</td>'+
      '<td class="num">'+wr+'</td>'+
      '<td class="num '+cls(w.real)+'">'+money(w.real)+'</td>'+
      '<td class="num '+cls(w.open)+'">'+money(w.open)+'</td>'+
      '<td class="num '+cls(w.total)+'">'+money(w.total)+'</td></tr>';
  }).join("") || '<tr><td colspan="8" class="empty">данных нет</td></tr>';
  $("skiprows").innerHTML = (d.rows||[]).map(r=>{
    const sp = r.shadow_pnl;
    const spc = sp==null ? '<span class="muted">—</span>' : '<span class="num '+cls(sp)+'">'+money(sp)+'</span>';
    const stcls = "st-"+r.status, stlbl = {win:"выигрыш",lose:"проигрыш",open:"открыта"}[r.status]||r.status;
    const gem = (r.status==="win") ? " gem" : "";
    const outc = r.status==="open" ? (r.mark!=null?r.mark.toFixed(3):"—") : (r.status==="win"?"$1.00":"$0.00");
    return '<tr class="'+gem+'"><td class="mono muted">'+tm(r.t)+'</td>'+
      '<td>'+addrLink(r.w)+'</td>'+
      '<td><span class="rtag">'+(REASON_LBL[r.reason]||r.reason)+'</span></td>'+
      '<td class="title">'+(r.title||"")+'</td>'+
      '<td class="num">'+(r.entry!=null?r.entry.toFixed(3):"—")+'</td>'+
      '<td class="num muted">'+(r.their_px!=null?r.their_px.toFixed(3):"—")+'</td>'+
      '<td class="num muted">'+outc+'</td>'+
      '<td class="'+stcls+'">'+stlbl+'</td>'+
      '<td>'+spc+'</td></tr>';
  }).join("") || '<tr><td colspan="9" class="empty">отфильтрованных сделок пока нет</td></tr>';
}
tick(); setInterval(tick, 10000);
setInterval(()=>{ if(skipOpen) loadSkipped(); }, 10000);
setInterval(()=>{ if(curWallet) loadWallet(); }, 10000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/state")
def api_state():
    with _lock:
        book = STATE["book"]
        marks = dict(STATE["marks"])
        status = dict(STATE["status"])
    data = compute_stats(book, marks)
    data["status"] = status
    return jsonify(data)


@app.route("/api/skipped")
def api_skipped():
    """Журнал отфильтрованных сделок + теневой PnL: что было бы, если бы фильтр их пропустил."""
    with _lock:
        book = STATE["book"]
        marks = dict(STATE["marks"])
    sk = list(book.get("skipped", []))
    realized = book.get("skipped_realized", 0.0)
    open_pnl = 0.0
    resolved_n = wins = 0
    by_reason: dict = {}
    from collections import defaultdict
    wstat = defaultdict(lambda: {"n": 0, "resolved": 0, "wins": 0, "real": 0.0, "open": 0.0})
    rows = []
    for r in sk:
        rsn = r.get("reason", "?")
        by_reason[rsn] = by_reason.get(rsn, 0) + 1
        w = r.get("w", "")
        ws = wstat[w]
        ws["n"] += 1
        mk = marks.get(r["tok"])
        if r.get("resolved"):
            resolved_n += 1
            spnl = r.get("pnl")
            ws["resolved"] += 1
            ws["real"] += (spnl or 0)
            if (spnl or 0) > 0:
                wins += 1
                ws["wins"] += 1
            status = "win" if (spnl or 0) > 0 else "lose"
        else:
            spnl = round(r["qty"] * mk - r["notional"], 2) if (mk is not None and mk > 0) else None
            if spnl is not None:
                open_pnl += spnl
                ws["open"] += spnl
            status = "open"
        rows.append({"t": r.get("t", 0), "w": w, "reason": rsn,
                     "title": r.get("title", ""), "entry": r.get("entry"),
                     "their_px": r.get("their_px"), "mark": mk,
                     "notional": r.get("notional"), "status": status, "shadow_pnl": spnl})
    # хидден-гемы наверх: сначала с известным PnL по убыванию
    rows.sort(key=lambda x: (x["shadow_pnl"] is None, -(x["shadow_pnl"] or 0)))
    src = get_sources()
    per_wallet = [{"wallet": w, "source": src.get(w.lower(), "—"), "n": s["n"],
                   "resolved": s["resolved"], "wins": s["wins"],
                   "real": round(s["real"], 2), "open": round(s["open"], 2),
                   "total": round(s["real"] + s["open"], 2)} for w, s in wstat.items()]
    per_wallet.sort(key=lambda x: x["total"], reverse=True)   # кто плюсует на отфильтрованных — наверх
    return jsonify({
        "count": len(sk), "realized": round(realized, 2), "open_pnl": round(open_pnl, 2),
        "resolved_n": resolved_n, "wins": wins, "by_reason": by_reason,
        "per_wallet": per_wallet, "rows": rows[:200],
    })


@app.route("/api/wallet")
def api_wallet():
    """Детализация по одному кошельку: скопированные сделки + открытые позиции."""
    addr = (request.args.get("addr") or "").lower()
    with _lock:
        book = STATE["book"]
        marks = dict(STATE["marks"])

    def mark_val(p):
        mk = marks.get(p["token"])
        return p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]

    # время открытия: из поля opened, для старых позиций — самый ранний BUY из лога
    first_buy = {}
    for r in book["log"]:
        if r.get("act") == "BUY" and (r.get("w", "") or "").lower() == addr:
            k = (r.get("title", ""), r.get("out", ""))
            t = r.get("t", 0)
            if t and (k not in first_buy or t < first_buy[k]):
                first_buy[k] = t
    positions = []
    for p in book["positions"].values():
        if (p.get("wallet", "") or "").lower() != addr:
            continue
        v = mark_val(p)
        entry = (p["cost"] / p["qty"]) if p.get("qty") else None
        opened = p.get("opened") or first_buy.get(((p.get("title", "") or "")[:46], p.get("outcome", "")))
        positions.append({"title": p.get("title", ""), "outcome": p.get("outcome", ""),
                          "entry": round(entry, 4) if entry else None,
                          "mark": marks.get(p["token"]), "cost": round(p["cost"], 2),
                          "val": round(v, 2), "pnl": round(v - p["cost"], 2),
                          "fills": p.get("fills", 1), "opened": opened})
    positions.sort(key=lambda x: x["cost"], reverse=True)

    wl = [r for r in book["log"] if (r.get("w", "") or "").lower() == addr]
    spent = sum(r.get("spend", 0) or 0 for r in wl if r.get("act") == "BUY")
    realized = sum(r.get("pnl", 0) or 0 for r in wl if "pnl" in r)
    # лог с меткой вход/выход (BUY=вход; SELL/REDEEM/SETTLE=выход)
    log = [{"t": r.get("t", 0), "act": r.get("act", ""), "out": r.get("out", ""),
            "title": r.get("title", ""), "pnl": r.get("pnl"), "spend": r.get("spend"),
            "px": r.get("px"), "kind": ("close" if "pnl" in r else "open")}
           for r in wl[-120:][::-1]]

    # ЗАКРЫТЫЕ позиции: рынки, где есть закрывающие сделки и НЕТ открытой позиции сейчас
    open_keys = {((p["title"] or "")[:46], p["outcome"]) for p in positions}
    groups: dict = {}
    for r in wl:
        k = (r.get("title", ""), r.get("out", ""))
        g = groups.setdefault(k, {"invested": 0.0, "realized": 0.0, "buys": 0, "closes": 0,
                                  "title": r.get("title", ""), "out": r.get("out", "")})
        if r.get("act") == "BUY":
            g["invested"] += r.get("spend", 0) or 0
            g["buys"] += 1
        if "pnl" in r:
            g["realized"] += r.get("pnl", 0) or 0
            g["closes"] += 1
    closed = [{"title": g["title"], "out": g["out"], "invested": round(g["invested"], 2),
               "realized": round(g["realized"], 2), "buys": g["buys"],
               "result": "win" if g["realized"] > 0 else ("lose" if g["realized"] < 0 else "flat")}
              for k, g in groups.items() if g["closes"] > 0 and k not in open_keys]
    closed.sort(key=lambda x: x["realized"], reverse=True)

    # теневые (отфильтрованные) сделки этого кошелька — сводка + список строк
    sh = {"n": 0, "resolved": 0, "wins": 0, "real": 0.0, "open": 0.0}
    shadow_rows = []
    for r in book.get("skipped", []):
        if (r.get("w", "") or "").lower() != addr:
            continue
        sh["n"] += 1
        mk = marks.get(r["tok"])
        if r.get("resolved"):
            sh["resolved"] += 1
            spnl = r.get("pnl")
            sh["real"] += (spnl or 0)
            status = "win" if (spnl or 0) > 0 else "lose"
            if (spnl or 0) > 0:
                sh["wins"] += 1
        else:
            spnl = round(r["qty"] * mk - r["notional"], 2) if (mk is not None and mk > 0) else None
            if spnl is not None:
                sh["open"] += spnl
            status = "open"
        shadow_rows.append({"t": r.get("t", 0), "title": r.get("title", ""), "out": r.get("outcome", ""),
                            "reason": r.get("reason", ""), "entry": r.get("entry"),
                            "status": status, "shadow_pnl": spnl})
    shadow_rows.sort(key=lambda x: (x["shadow_pnl"] is None, -(x["shadow_pnl"] or 0)))

    return jsonify({"wallet": addr, "source": get_sources().get(addr, "—"),
                    "n_buys": sum(1 for r in wl if r.get("act") == "BUY"),
                    "n_open": len(positions), "spent": round(spent, 2),
                    "realized": round(realized, 2),
                    "unrealized": round(sum(p["pnl"] for p in positions), 2),
                    "shadow": {"n": sh["n"], "resolved": sh["resolved"], "wins": sh["wins"],
                               "real": round(sh["real"], 2), "open": round(sh["open"], 2),
                               "total": round(sh["real"] + sh["open"], 2)},
                    "n_closed": len(closed),
                    "positions": positions, "closed": closed[:80], "log": log,
                    "shadow_rows": shadow_rows[:120]})


@app.route("/api/remove_wallet", methods=["POST"])
def api_remove_wallet():
    """Удалить кошелёк из watchlist (копирование новых сделок прекращается; открытые позиции
    до-резолвятся сами через независимый оракул). Файл watchlist перечитывается на лету."""
    data = request.get_json(silent=True) or {}
    if hashlib.sha256((data.get("pw", "") or "").encode("utf-8")).hexdigest() != ADMIN_HASH:
        return jsonify({"ok": False, "error": "auth"}), 401
    addr = (data.get("wallet", "") or "").lower()
    if not addr:
        return jsonify({"ok": False, "error": "no wallet"}), 400
    path = Path(STATE.get("wl_path") or "copy_watchlist.json")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    before = len(d.get("watchlist", []))
    d["watchlist"] = [w for w in d.get("watchlist", []) if w.lower() != addr]
    d["count"] = len(d["watchlist"])
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:                                                       # пометка в источниках (для истории)
        sp = Path("wallet_sources.json")
        s = json.loads(sp.read_text(encoding="utf-8"))
        s[addr] = "удалён-вручную"
        sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "removed": before - d["count"], "count": d["count"]})


def main():
    p = argparse.ArgumentParser(description="Веб-дашборд бумажного копи")
    p.add_argument("--wallets", help="адреса целей через запятую")
    p.add_argument("--from-watchlist", help="взять цели из ranked_watchlist.json")
    p.add_argument("--bankroll", type=float, default=100_000)
    p.add_argument("--per-trade", type=float, default=100)
    p.add_argument("--slippage", type=float, default=0.01)
    p.add_argument("--state", default="paper_book.json")
    p.add_argument("--interval", type=int, default=120, help="период опроса, сек (дефолт 120)")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 для доступа на сервере (за фаерволом/SSH)")
    args = p.parse_args()

    wallets = ct.resolve_wallets(args)
    if not wallets:
        print("Не заданы цели. --wallets addr1,addr2 или --from-watchlist ranked_watchlist.json")
        return
    wallets = [w.lower() for w in wallets]

    STATE["book"] = ct.load_book(args.state, args.bankroll)
    STATE["wl_path"] = args.from_watchlist          # путь watchlist для кнопки удаления / фильтра рейтинга
    STATE["status"]["wallets"] = len(wallets)
    STATE["cfg"] = {"interval": args.interval, "per_trade": args.per_trade, "slippage": args.slippage}

    api = ct.API()
    t = threading.Thread(target=poll_loop,
                         args=(api, wallets, args.per_trade, args.slippage, args.interval,
                               args.state, args.from_watchlist),
                         daemon=True)
    t.start()

    print(f"дашборд: http://localhost:{args.port}  | целей {len(wallets)}, опрос каждые {args.interval}s")
    print("первый цикл зафиксирует старт (копирование со второго). Ctrl+C для остановки.")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
