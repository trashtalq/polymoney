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
    "book_ver": 0,     # растёт при любой прямой правке книги в обход poll_loop (purge/удаление кошелька)
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
                                "open_val": 0.0, "open_cost": 0.0, "spent": 0.0, "open_n": 0,
                                "delay": 0.0})
    for r in book["log"]:
        w = r.get("w", "?")
        if r.get("act") == "BUY":
            stat[w]["copied"] += 1
            stat[w]["spent"] += r.get("spend", 0) or 0   # суммарно задействовано $ на эту цель
            px, tp = r.get("px"), r.get("their_px")      # задержка-кост: переплата против цены цели
            if px and tp and px > 0:
                stat[w]["delay"] += (r.get("spend", 0) or 0) * (px - tp) / px
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
            "delay": round(s["delay"], 2),            # $ переплаты за задержку копира на входах
            "flag": flag,
            "source": sources.get(w, "—"),
        })
    # ---- PnL «за сегодня»: ЕДИНЫЙ дневной снимок ПО КОШЕЛЬКАМ (сброс в полночь сервера) ----
    # Храним ТОЛЬКО пер-кошельковый срез на начало суток. Карточку считаем как сумму «сегодня»
    # по видимым кошелькам — тогда верх и таблица всегда сходятся, и удаление кошелька всегда
    # двигает карточку ровно на его дневной вклад. Отдельный total_pnl-скаляр держать нельзя:
    # при добавлении кошелька после полуночи он расходится с суммой по кошелькам (был баг).
    today_str = time.strftime("%Y-%m-%d")
    base = book.get("day_baseline")
    if not base or base.get("date") != today_str:             # новый день -> фиксируем точку отсчёта
        base = {"date": today_str,
                "per_wallet": {w["wallet"]: w["total"] for w in per_wallet}}
        book["day_baseline"] = base
    bw = base.setdefault("per_wallet", {})
    for w in per_wallet:                                       # сколько кошелёк прибавил/потерял с начала суток
        w["today"] = round(w["total"] - bw.get(w["wallet"], w["total"]), 2)

    wlset = get_watchlist()
    if wlset:                                                  # скрываем удалённые из рейтинга (позиции их доживут сами)
        per_wallet = [w for w in per_wallet if w["wallet"].lower() in wlset]
    per_wallet.sort(key=lambda x: x["total"], reverse=True)   # авто-ранжирование по форвардному PnL
    pnl_today = round(sum(w["today"] for w in per_wallet), 2)  # карточка = сумма «сегодня» видимых кошельков

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

    # ---- реал-леджер: что поймал бы НАСТОЯЩИЙ кэш (без доливов) ----
    ra = book.get("realacct") or {}
    real = None
    if ra:
        r_unreal = 0.0
        for p in book["positions"].values():
            rc, c = (p.get("rcost", 0.0) or 0.0), (p.get("cost", 0.0) or 0.0)
            if rc > 0 and c > 0:
                r_unreal += (mark_val(p) - c) * min(1.0, rc / c)
        n_att = ra.get("taken", 0) + ra.get("missed", 0)
        real = {"base": ra.get("base"), "cash": round(ra.get("cash", 0.0), 2),
                "realized": round(ra.get("realized", 0.0), 2), "unrealized": round(r_unreal, 2),
                "pnl": round(ra.get("realized", 0.0) + r_unreal, 2),
                "taken": ra.get("taken", 0), "missed": ra.get("missed", 0),
                "missed_pct": round(100.0 * ra.get("missed", 0) / n_att, 1) if n_att else 0.0,
                "missed_spend": round(ra.get("missed_spend", 0.0), 2)}

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
        "pnl_today": pnl_today,
        "real": real,
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
                ver0 = STATE["book_ver"]                    # версия книги на момент снимка
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
                if STATE["book_ver"] != ver0:
                    # книгу правили извне (purge/удаление кошелька) пока мы опрашивали API —
                    # наш снимок устарел, коммитить его нельзя (иначе откатим правку). Пропускаем
                    # цикл целиком, на следующем круге просто повторим (seen не продвинулись).
                    STATE["status"]["polling"] = False
                    STATE["status"]["error"] = ""
                else:
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
<title>POLYMONEY — копи-терминал</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='88'>⚡</text></svg>">
<style>
  :root{
    --bg:#04060b; --panel:#0b1220; --panel2:#0e1728;
    --border:rgba(70,130,160,.16); --border2:rgba(6,229,255,.30);
    --text:#d7eef2; --muted:#5f7e8e; --dim:#3d5866;
    --green:#2bf5b0; --red:#ff3b6b; --accent:#06e5ff; --violet:#8b7bff; --amber:#ffc23d;
  }
  *{box-sizing:border-box}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:#16283a;border-radius:6px;border:2px solid #04060b}
  ::-webkit-scrollbar-track{background:transparent}
  ::selection{background:rgba(6,229,255,.25)}
  body{margin:0;color:var(--text);font-size:14px;
       font-family:ui-monospace,"JetBrains Mono","Cascadia Code",SFMono-Regular,Menlo,Consolas,monospace;
       background:
         radial-gradient(1100px 560px at 85% -10%, rgba(6,229,255,.10), transparent 55%),
         radial-gradient(900px 520px at -10% 18%, rgba(139,123,255,.07), transparent 55%),
         radial-gradient(1000px 620px at 55% 115%, rgba(255,59,107,.06), transparent 60%),
         linear-gradient(180deg,#05070d 0%,#03050a 100%);
       background-attachment:fixed;min-height:100vh}
  body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
       background-image:linear-gradient(rgba(95,126,142,.05) 1px,transparent 1px),
                        linear-gradient(90deg,rgba(95,126,142,.05) 1px,transparent 1px);
       background-size:44px 44px;
       -webkit-mask-image:radial-gradient(1200px 780px at 50% 0%,#000 25%,transparent 100%);
       mask-image:radial-gradient(1200px 780px at 50% 0%,#000 25%,transparent 100%)}
  body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
       background:repeating-linear-gradient(0deg,rgba(0,0,0,0) 0 2px,rgba(0,0,0,.10) 2px 3px);opacity:.35}
  .num{font-variant-numeric:tabular-nums}
  .wrap{max-width:1160px;margin:0 auto;padding:24px 20px 70px;position:relative;z-index:1}
  header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:18px}
  h1{font-size:15px;font-weight:800;letter-spacing:3px;margin:0;text-transform:uppercase;
     background:linear-gradient(90deg,#7ff3ff,var(--accent) 55%,#4aa9ff);
     -webkit-background-clip:text;background-clip:text;color:transparent;
     filter:drop-shadow(0 0 12px rgba(6,229,255,.35))}
  h1 .h1sub{font-weight:500;letter-spacing:1px;font-size:11px;color:var(--muted);-webkit-text-fill-color:var(--muted)}
  h1 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--dim);margin-right:10px;vertical-align:middle}
  h1 .dot.live{background:var(--green);box-shadow:0 0 10px var(--green),0 0 24px var(--green);animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  .status{color:var(--muted);font-size:12px;padding:6px 14px;border:1px solid var(--border);
          border-radius:999px;background:rgba(10,18,30,.55);backdrop-filter:blur(8px)}
  .status b{color:var(--accent);font-weight:700}
  .heroLabel{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:2.5px;margin-bottom:6px}
  .hero{font-size:58px;font-weight:800;letter-spacing:-2px;line-height:1;margin:0}
  .hero.pos{background:linear-gradient(100deg,#8dffe0,var(--green) 45%,var(--accent));
            -webkit-background-clip:text;background-clip:text;color:transparent;
            filter:drop-shadow(0 0 26px rgba(43,245,176,.30))}
  .hero.neg{background:linear-gradient(100deg,#ff9db3,var(--red) 50%,#ff7b4f);
            -webkit-background-clip:text;background-clip:text;color:transparent;
            filter:drop-shadow(0 0 26px rgba(255,59,107,.30))}
  .hero.zero{color:var(--muted)}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
  .chip{padding:4px 12px;border-radius:999px;border:1px solid var(--border);
        background:rgba(10,18,30,.55);font-size:12px;color:var(--muted)}
  .chip b{color:var(--text);font-weight:700}
  .chip.pos b{color:var(--green)} .chip.neg b{color:var(--red)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:22px 0}
  .card{position:relative;border-radius:14px;padding:14px 16px;overflow:hidden;
        background:linear-gradient(160deg,rgba(17,27,44,.92),rgba(9,15,26,.92));
        border:1px solid var(--border);
        box-shadow:0 10px 28px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.035);
        transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease}
  .card:hover{transform:translateY(-2px);border-color:var(--border2);
        box-shadow:0 14px 34px rgba(0,0,0,.45),0 0 20px rgba(6,229,255,.08)}
  .card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
        border-radius:14px 0 0 14px;background:linear-gradient(180deg,var(--accent),transparent);opacity:.75}
  .card.feat::before{background:linear-gradient(180deg,var(--green),var(--accent))}
  .card.vio::before{background:linear-gradient(180deg,var(--violet),transparent)}
  .card.feat{border-color:rgba(43,245,176,.28)}
  .card .k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1.2px}
  .card .v{font-size:23px;font-weight:800;margin-top:6px;letter-spacing:-.5px}
  .card .v.pos{text-shadow:0 0 14px rgba(43,245,176,.35)}
  .card .v.neg{text-shadow:0 0 14px rgba(255,59,107,.35)}
  .card.clickable{cursor:pointer}
  .card.clickable:hover{border-color:var(--accent)}
  .card .hint{color:var(--accent);font-size:10px;margin-top:7px;letter-spacing:.4px;opacity:.85}
  .sec{display:flex;align-items:center;gap:11px;margin:32px 0 12px;font-size:12px;
       letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);
       text-shadow:0 0 10px rgba(6,229,255,.35)}
  .sec::before{content:"";width:16px;height:3px;border-radius:2px;flex:none;
       background:linear-gradient(90deg,var(--accent),transparent);box-shadow:0 0 8px var(--accent)}
  .sec::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,rgba(6,229,255,.22),transparent)}
  .tblwrap{border:1px solid var(--border);border-radius:14px;overflow:auto;
       background:linear-gradient(180deg,rgba(14,23,38,.78),rgba(9,15,26,.85));
       box-shadow:0 10px 28px rgba(0,0,0,.3)}
  table{width:100%;border-collapse:collapse}
  .tblwrap table{min-width:660px}
  .tblwrap.wide table{min-width:1120px}
  th,td{padding:10px 12px;text-align:right;border-bottom:1px solid rgba(70,130,160,.09);white-space:nowrap}
  th:first-child,td:first-child{text-align:left;padding-left:16px}
  th{position:sticky;top:0;z-index:2;color:var(--muted);font-size:10px;text-transform:uppercase;
     letter-spacing:1.2px;font-weight:700;background:rgba(10,17,29,.96);
     border-bottom:1px solid var(--border2);backdrop-filter:blur(6px)}
  tr:last-child td{border-bottom:none}
  tbody tr{transition:background .12s ease}
  tbody tr:nth-child(even){background:rgba(255,255,255,.014)}
  tbody tr:hover{background:rgba(6,229,255,.055)}
  tbody tr:hover td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
  td.rank{color:var(--dim);font-size:12px;font-weight:700}
  td.rank.r1{color:#ffd54d;text-shadow:0 0 10px rgba(255,213,77,.55)}
  td.rank.r2{color:#d5e3ea;text-shadow:0 0 8px rgba(213,227,234,.35)}
  td.rank.r3{color:#ffab73;text-shadow:0 0 8px rgba(255,171,115,.4)}
  .wr{display:inline-flex;align-items:center;gap:7px}
  .wr i{display:block;height:5px;border-radius:3px;min-width:3px}
  .wr b{font-weight:700;font-size:12px}
  .pos{color:var(--green);text-shadow:0 0 8px rgba(43,245,176,.4)}
  .neg{color:var(--red);text-shadow:0 0 8px rgba(255,59,107,.4)} .zero{color:var(--muted)}
  .addr{font-family:inherit;color:var(--accent);font-size:12px}
  .addr.clk{cursor:pointer;border-bottom:1px dotted rgba(6,229,255,.5)}
  .addr.clk:hover{color:#fff;border-bottom-color:#fff}
  .del{cursor:pointer;color:var(--dim);font-weight:700;padding:2px 8px;border-radius:7px;transition:all .12s}
  .del:hover{color:#fff;background:rgba(255,59,107,.3);box-shadow:0 0 10px rgba(255,59,107,.3)}
  .title{color:var(--text);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tag{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.3px}
  .tag.BUY{background:rgba(6,229,255,.12);color:var(--accent);box-shadow:inset 0 0 0 1px rgba(6,229,255,.25)}
  .tag.SELL{background:rgba(255,194,61,.12);color:var(--amber);box-shadow:inset 0 0 0 1px rgba(255,194,61,.25)}
  .tag.REDEEM,.tag.SETTLE{background:rgba(43,245,176,.10);color:var(--green);box-shadow:inset 0 0 0 1px rgba(43,245,176,.22)}
  .tag.YES{background:rgba(43,245,176,.10);color:var(--green);box-shadow:inset 0 0 0 1px rgba(43,245,176,.22)}
  .tag.NO{background:rgba(255,59,107,.10);color:var(--red);box-shadow:inset 0 0 0 1px rgba(255,59,107,.22)}
  .empty{color:var(--muted);padding:20px 6px;font-style:italic}
  .spark{border:1px solid var(--border);border-radius:14px;padding:14px 14px 8px;
       background:linear-gradient(180deg,rgba(14,23,38,.78),rgba(9,15,26,.85));
       box-shadow:0 10px 28px rgba(0,0,0,.3)}
  .spark svg .ping{transform-box:fill-box;transform-origin:center;animation:ping 2.2s ease-out infinite}
  @keyframes ping{0%{transform:scale(1);opacity:.9}75%{transform:scale(3.4);opacity:0}100%{opacity:0}}
  .muted{color:var(--muted)} .err{color:var(--red);font-size:12px;margin-top:6px}
  .mono{font-family:inherit}
  .modal-bg{display:none;position:fixed;inset:0;background:rgba(2,6,12,.72);z-index:50;
       padding:34px 16px;overflow:auto;backdrop-filter:blur(5px)}
  .modal-bg.show{display:block}
  .modal{max-width:1000px;margin:0 auto;border-radius:18px;padding:20px 22px;
       background:linear-gradient(165deg,rgba(16,26,42,.97),rgba(8,13,23,.97));
       border:1px solid var(--border2);box-shadow:0 30px 80px rgba(0,0,0,.6),0 0 40px rgba(6,229,255,.06);
       animation:pop .16s ease}
  @keyframes pop{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}
  .modal table{border:1px solid var(--border);border-radius:10px;overflow:hidden}
  .modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
  .modal-head b{font-size:15px}
  .x{cursor:pointer;color:var(--muted);font-size:18px;padding:2px 9px;border-radius:8px}
  .x:hover{color:var(--text);background:rgba(255,255,255,.06)}
  .skipsum{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:9px;margin:12px 0 16px}
  .skipsum .b{background:rgba(14,23,40,.85);border:1px solid var(--border);border-radius:11px;padding:10px 12px}
  .skipsum .b .k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
  .skipsum .b .v{font-size:18px;font-weight:700;margin-top:4px}
  .rtag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;
       background:rgba(20,32,50,.8);color:var(--muted);box-shadow:inset 0 0 0 1px rgba(70,130,160,.18)}
  .st-win{color:var(--green)} .st-lose{color:var(--red)} .st-open{color:var(--muted)}
  .gem{background:rgba(43,245,176,.07)}
  @media (max-width:640px){.hero{font-size:42px}.wrap{padding:16px 12px 50px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span id="dot" class="dot"></span>POLYMONEY <span class="h1sub">· копи-терминал</span></h1>
    <div class="status" id="status">подключение…</div>
  </header>

  <div class="heroLabel">Итого PnL — форвард</div>
  <div class="hero num" id="hero">—</div>
  <div class="chips" id="chips"></div>
  <div class="err" id="err"></div>

  <div class="cards" id="cards"></div>

  <div class="sec">Кривая PnL</div>
  <div class="spark"><svg id="spark" width="100%" height="132" preserveAspectRatio="none"></svg></div>

  <div class="sec">По кошелькам — авто-ранжирование по форвардному PnL</div>
  <div class="tblwrap wide">
  <table>
    <thead><tr><th>#</th><th>Кошелёк</th><th>Источник</th><th>Скопир.</th><th>Задейств.</th><th>Закрыто</th><th>Открыто</th><th>Винрейт</th><th>Реализ. PnL</th><th>Нереализ. PnL</th><th>PnL итого</th><th>За сегодня</th><th title="переплата против цены цели из-за задержки копирования">Задержка$</th><th>Статус</th><th></th></tr></thead>
    <tbody id="wallets"></tbody>
  </table>
  </div>

  <div class="sec">Открытые позиции — все</div>
  <div class="tblwrap">
  <table>
    <thead><tr><th>Кошелёк</th><th>Рынок</th><th>Ставка</th><th>Вход</th><th>Тек. кэф</th><th>Вложено</th><th>Оценка</th><th>P/L</th></tr></thead>
    <tbody id="open"></tbody>
  </table>
  </div>

  <div class="sec">Последние действия</div>
  <div class="tblwrap">
  <table>
    <thead><tr><th>Время</th><th>Кошелёк</th><th>Действие</th><th>Ставка</th><th>Рынок</th><th>PnL / сумма</th></tr></thead>
    <tbody id="log"></tbody>
  </table>
  </div>
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
  if(!confirm("Удалить кошелёк "+shortAddr(a)+"?\nКнига пересчитается заново, как будто его никогда не было: все его позиции, сделки и PnL уберутся безвозвратно.")) return;
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
  const svg=$("spark"); const W=svg.clientWidth||1000, H=132, pad=10, padR=64, padB=18;
  svg.innerHTML="";
  if(!hist||hist.length<2){ svg.innerHTML='<text x="14" y="70" fill="#5f7e8e" font-size="12">данных пока нет</text>'; return; }
  let pts=hist;                                        // прореживание: рисуем максимум ~700 точек
  if(pts.length>700){ const k=Math.ceil(pts.length/700);
    pts=pts.filter((_,i)=>i%k===0); if(pts[pts.length-1]!==hist[hist.length-1]) pts.push(hist[hist.length-1]); }
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[2]);      // total pnl
  const x0=Math.min(...xs), x1=Math.max(...xs);
  let y0=Math.min(...ys,0), y1=Math.max(...ys,0);
  const span=(y1-y0)||2; y0-=span*.07; y1+=span*.07;
  const X=t=>pad+(W-pad-padR)*(x1===x0?0.5:(t-x0)/(x1-x0));
  const Y=v=>pad+(H-pad-padB)*(1-(v-y0)/(y1-y0));
  const last=ys[ys.length-1];
  const col=last>0?"#2bf5b0":(last<0?"#ff3b6b":"#8b949e");
  const fmt=v=>(v<0?"-":"")+"$"+Math.abs(Math.round(v)).toLocaleString("en-US");
  let g='<defs><linearGradient id="pg" x1="0" y1="0" x2="0" y2="1">'+
        '<stop offset="0%" stop-color="'+col+'" stop-opacity="0.28"/>'+
        '<stop offset="100%" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>';
  for(let i=1;i<=3;i++){ const gy=pad+(H-pad-padB)*i/4;    // сетка
    g+='<line x1="'+pad+'" y1="'+gy+'" x2="'+(W-padR)+'" y2="'+gy+'" stroke="rgba(95,126,142,.10)"/>'; }
  const yz=Y(0);
  if(yz>pad&&yz<H-padB)
    g+='<line x1="'+pad+'" y1="'+yz+'" x2="'+(W-padR)+'" y2="'+yz+'" stroke="rgba(95,126,142,.35)" stroke-dasharray="4 4"/>'+
       '<text x="'+(W-padR+6)+'" y="'+(yz+4)+'" fill="#3d5866" font-size="10">$0</text>';
  let d="M "+X(xs[0]).toFixed(1)+" "+Y(ys[0]).toFixed(1);
  for(let i=1;i<pts.length;i++) d+=" L "+X(xs[i]).toFixed(1)+" "+Y(ys[i]).toFixed(1);
  const lx=X(xs[xs.length-1]), ly=Y(last);
  g+='<path d="'+d+' L '+lx.toFixed(1)+' '+(H-padB)+' L '+X(xs[0]).toFixed(1)+' '+(H-padB)+' Z" fill="url(#pg)"/>';
  g+='<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round" '+
     'style="filter:drop-shadow(0 0 6px '+col+')"/>';
  g+='<circle class="ping" cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="4" fill="'+col+'" opacity=".7"/>';
  g+='<circle cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="3.5" fill="'+col+'" '+
     'style="filter:drop-shadow(0 0 8px '+col+')"/>';
  g+='<text x="'+(W-padR+6)+'" y="'+Math.max(pad+9,Math.min(H-padB-2,ly+4))+'" fill="'+col+'" font-size="11" font-weight="700">'+fmt(last)+'</text>';
  const yMax=Math.max(...ys), yMin=Math.min(...ys);
  if(Math.abs(Y(yMax)-ly)>14) g+='<text x="'+(W-padR+6)+'" y="'+(Y(yMax)+4)+'" fill="#3d5866" font-size="10">'+fmt(yMax)+'</text>';
  if(Math.abs(Y(yMin)-ly)>14&&Math.abs(Y(yMin)-Y(yMax))>14) g+='<text x="'+(W-padR+6)+'" y="'+(Y(yMin)+4)+'" fill="#3d5866" font-size="10">'+fmt(yMin)+'</text>';
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
  $("hero").textContent = money(d.pnl);
  const chip=(k,v,c)=>'<span class="chip '+(c||"")+'">'+k+' <b>'+v+'</b></span>';
  $("chips").innerHTML =
    chip("реализовано",money(d.realized),cls(d.realized))+
    chip("за сегодня",money(d.pnl_today||0),cls(d.pnl_today||0))+
    chip("ROI",(d.roi*100).toFixed(1)+"%",cls(d.roi))+
    (d.real?chip("реал-кэш",money(d.real.pnl||0),cls(d.real.pnl||0)):"");

  // [метка, значение, класс значения, клик-журнал, класс карточки]
  const cardsArr = [
    ["PnL за сегодня", money(d.pnl_today||0), cls(d.pnl_today||0), "", "feat"],
    ["Реализовано (закрытые)", money(d.realized), cls(d.realized)],
    ["Нереализ. PnL (открытые)", money(d.unrealized), cls(d.unrealized)],
    ["Открытых позиций", d.n_open, ""],
    ["BUY-действий (с усредн.)", d.n_copied, ""],
    ["Вложено в открытые", money(d.invested_open), ""],
    ["Отфильтровано (защита EV)", d.n_skipped||0, "", "skip"],
    ["Свободный кэш", money(d.cash), ""],
    ["Долито капитала", money(d.topups||0), ""],
    ["Банкролл (с доливами)", money(d.bankroll), ""],
  ];
  if(d.real){
    cardsArr.splice(1, 0, ["Реал-PnL (кэш $"+((d.real.base||0)/1000).toFixed(1)+"k, без доливов)",
                           money(d.real.pnl||0), cls(d.real.pnl||0), "", "vio"]);
    cardsArr.push(["Пропущено входов (реал)", (d.real.missed||0)+" ("+(d.real.missed_pct||0)+"%)",
                   (d.real.missed_pct>25?"neg":""), "", "vio"]);
  }
  $("cards").innerHTML = cardsArr.map(c=>'<div class="card'+(c[4]?' '+c[4]:'')+(c[3]?' clickable" onclick="openSkipped()"':'"')+'><div class="k">'+c[0]+'</div><div class="v num '+c[2]+'">'+c[1]+'</div>'+(c[3]?'<div class="hint">открыть журнал ›</div>':'')+'</div>').join("");

  sparkline(d.pnl_history);

  const winbar = r => { const p=Math.round(r*100);
    const c = p>=60?"var(--green)":(p>=40?"var(--accent)":"var(--red)");
    return '<span class="wr"><i style="width:'+Math.max(3,Math.round(p*.45))+'px;background:'+c+
           ';box-shadow:0 0 7px '+c+'"></i><b>'+p+'%</b></span>'; };
  $("wallets").innerHTML = (d.per_wallet||[]).map((w,i)=>
    '<tr><td class="rank num'+(i<3?' r'+(i+1):'')+'">'+(i+1)+'</td>'+
    '<td>'+addrLink(w.wallet)+'</td>'+
    '<td><span class="rtag">'+(w.source||"—")+'</span></td>'+
    '<td class="num">'+w.copied+'</td>'+
    '<td class="num muted">'+money(w.spent)+'</td>'+
    '<td class="num">'+w.closed+'</td>'+
    '<td class="num">'+(w.open_n||0)+'</td>'+
    '<td class="num">'+(w.closed?winbar(w.win_rate):"—")+'</td>'+
    '<td class="num '+cls(w.realized)+'">'+money(w.realized)+'</td>'+
    '<td class="num '+cls(w.unrealized)+'">'+money(w.unrealized)+'</td>'+
    '<td class="num '+cls(w.total)+'">'+money(w.total)+(w.spent>0?' <span class="muted">('+(w.total/w.spent>=0?'+':'')+(w.total/w.spent*100).toFixed(0)+'%)</span>':'')+'</td>'+
    '<td class="num '+cls(w.today||0)+'">'+(w.today!=null?money(w.today):'—')+'</td>'+
    '<td class="num '+((w.delay||0)>(w.spent||0)*0.02?"neg":"muted")+'">'+money(w.delay||0)+'</td>'+
    '<td>'+flagBadge(w.flag)+'</td>'+
    '<td><span class="del" title="удалить кошелёк" onclick="removeWallet(\''+w.wallet+'\')">✕</span></td></tr>').join("")
    || '<tr><td colspan="15" class="empty">пока ничего не скопировано — ждём первые сделки целей</td></tr>';

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
const REASON_LBL = {band:"цена у края", adverse:"догон от цели", avg_up:"догон вверх", cap:"потолок позиции", sport:"футбол/спорт", weather:"погода"};
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
    """Удалить кошелёк из watchlist (копирование новых сделок прекращается) И пересчитать книгу
    «как будто его никогда не было» (purge_wallet): убрать его позиции/сделки/теневые записи,
    заново свести realized/bankroll/cash/topups. Файл watchlist перечитывается на лету."""
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
    with _lock:
        purge = ct.purge_wallet(STATE["book"], addr)
        STATE["book_ver"] += 1          # инвалидирует снимок, который мог снять poll_loop до этого
        try:
            ct.save_book(STATE.get("state_file", "paper_book.json"), STATE["book"])
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, "removed": before - d["count"], "count": d["count"], **purge})


@app.route("/api/rescale", methods=["POST"])
def api_rescale():
    """Однократный пересчёт ВСЕЙ книги в другой масштаб (напр. /100 — приближение к реалу).
    Идемпотентно: передаём ЦЕЛЕВОЙ банкролл (to_bankroll), множитель считается от текущего,
    поэтому повторный вызов с той же целью = множитель ~1 (без эффекта). Пароль в теле."""
    data = request.get_json(silent=True) or {}
    if hashlib.sha256((data.get("pw", "") or "").encode("utf-8")).hexdigest() != ADMIN_HASH:
        return jsonify({"ok": False, "error": "auth"}), 401
    with _lock:
        cur = float(STATE["book"].get("bankroll", 0.0) or 0.0)
        to = data.get("to_bankroll")
        factor = data.get("factor")
        if to is not None and cur > 0:
            factor = float(to) / cur
        if not factor or float(factor) <= 0:
            return jsonify({"ok": False, "error": "bad factor"}), 400
        res = ct.rescale_book(STATE["book"], float(factor))
        STATE["book_ver"] += 1          # инвалидирует снимок, который мог снять poll_loop до этого
        try:
            ct.save_book(STATE.get("state_file", "paper_book.json"), STATE["book"])
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, **res})


@app.route("/api/add_wallet", methods=["POST"])
def api_add_wallet():
    """Добавить кошельки в watchlist (пароль в теле): {pw, wallets:[...], source:"метка"}.
    Дедуп по текущему списку; вручную удалённые (wallet_sources=удалён-вручную) НЕ возвращаем
    (их выкинули осознанно), если не передан force=true. Hot-reload подхватит без рестарта."""
    data = request.get_json(silent=True) or {}
    if hashlib.sha256((data.get("pw", "") or "").encode("utf-8")).hexdigest() != ADMIN_HASH:
        return jsonify({"ok": False, "error": "auth"}), 401
    ws = [(w or "").lower().strip() for w in (data.get("wallets") or [])]
    ws = [w for w in ws if w.startswith("0x") and len(w) == 42]
    if not ws:
        return jsonify({"ok": False, "error": "no wallets"}), 400
    path = Path(STATE.get("wl_path") or "copy_watchlist.json")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    sp = Path("wallet_sources.json")
    try:
        srcs = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:  # noqa: BLE001
        srcs = {}
    have = {x.lower() for x in d.get("watchlist", [])}
    deleted = {w for w, s in srcs.items() if s == "удалён-вручную"}
    force = bool(data.get("force"))
    new = [w for w in dict.fromkeys(ws)                       # dict.fromkeys = дедуп с порядком
           if w not in have and (force or w not in deleted)]
    d["watchlist"].extend(new)
    d["count"] = len(d["watchlist"])
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    label = (data.get("source") or "добавлен-вручную")[:40]
    for w in new:
        srcs[w] = label
    try:
        sp.write_text(json.dumps(srcs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "added": len(new), "dup": sum(1 for w in ws if w in have),
                    "skipped_deleted": sum(1 for w in set(ws) - have if w in deleted and not force),
                    "count": d["count"], "wallets_added": new})


@app.route("/api/renorm", methods=["POST"])
def api_renorm():
    """Восстановление смешанного масштаба книги -> единый /10 (per_trade=10). Пароль в теле.
    Нужно один раз после того, как PER_TRADE наконец доехал на сервер (см. renorm_book)."""
    data = request.get_json(silent=True) or {}
    if hashlib.sha256((data.get("pw", "") or "").encode("utf-8")).hexdigest() != ADMIN_HASH:
        return jsonify({"ok": False, "error": "auth"}), 401
    tb = float(data.get("target_base", 10000.0))
    with _lock:
        res = ct.renorm_book(STATE["book"], target_base=tb)
        STATE["book_ver"] += 1          # инвалидирует снимок, который мог снять poll_loop до этого
        try:
            ct.save_book(STATE.get("state_file", "paper_book.json"), STATE["book"])
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, **res})


@app.route("/api/purge_blocked", methods=["POST"])
def api_purge_blocked():
    """Пересчитать книгу как будто футбол/погода никогда не копировались (пароль в теле)."""
    data = request.get_json(silent=True) or {}
    if hashlib.sha256((data.get("pw", "") or "").encode("utf-8")).hexdigest() != ADMIN_HASH:
        return jsonify({"ok": False, "error": "auth"}), 401
    with _lock:
        res = ct.purge_blocked(STATE["book"])
        STATE["book_ver"] += 1          # инвалидирует снимок, который мог снять poll_loop до этого
        try:
            ct.save_book(STATE.get("state_file", "paper_book.json"), STATE["book"])
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, **res})


# ----------------------------- отчёт для прополки -----------------------------
_PH = {"mtime": 0, "then": {}, "t_then": 0}


def _hist_then(days: float = 7.0) -> dict:
    """Срез total по кошелькам ~N дней назад из perf_history (для тренда). Кэш по mtime файла."""
    p = Path("perf_history_5000.jsonl")
    try:
        m = p.stat().st_mtime
    except OSError:
        return {}
    if m != _PH["mtime"]:
        target = time.time() - days * 86400
        best = None
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("t", 0) >= target:              # первый снимок не старше N дней
                best = rec
                break
        _PH.update({"mtime": m, "t_then": (best or {}).get("t", 0),
                    "then": {(w.get("w") or "").lower(): (w.get("total") or 0)
                             for w in (best or {}).get("wallets", [])}})
    return _PH["then"]


@app.route("/api/prune")
def api_prune():
    """Отчёт для еженедельной прополки: все форвардные метрики + задержка-кост + тренд ~7д.
    Кандидат на отсев: total<0 И trend7d<0 И closed>=8; отдельно — у кого задержка съедает эдж."""
    with _lock:
        book = STATE["book"]
        marks = dict(STATE["marks"])
    data = compute_stats(book, marks)
    then = _hist_then()
    rows = []
    for w in data["per_wallet"]:
        addr = w["wallet"].lower()
        d7 = round(w["total"] - then[addr], 2) if addr in then else None
        verdict = ""
        if w["closed"] >= 8:
            if w["total"] < 0 and (d7 is None or d7 <= 0):
                verdict = "отсев"                      # стабильно минусовой, тренд не спасает
            elif w["total"] <= 0 and w["delay"] > abs(w["total"]):
                verdict = "задержка-съедает"           # эдж есть, но не переживает копирование
            elif w["total"] > 0:
                verdict = "держать"
        rows.append({**w, "trend7d": d7, "verdict": verdict})
    rows.sort(key=lambda x: x["total"])               # худшие сверху — их и полем
    return jsonify({"t_then": _PH["t_then"], "n": len(rows), "rows": rows})


@app.route("/api/category")
def api_category():
    """PnL по категории рынков (kw=слово1,слово2). Реализ.(из лога) + нереализ.(по марку) + % на задействованное."""
    kws = [k.strip().lower() for k in (request.args.get("kw", "")).split(",") if k.strip()]
    if not kws:
        return jsonify({"error": "no kw"}), 400
    with _lock:
        book = STATE["book"]
        marks = dict(STATE["marks"])

    def match(t):
        t = (t or "").lower()
        return any(k in t for k in kws)

    def mval(p):
        mk = marks.get(p["token"])
        return p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]

    inv_open = val_open = 0.0
    n_open = 0
    for p in book["positions"].values():
        if match(p.get("title")):
            inv_open += p["cost"]
            val_open += mval(p)
            n_open += 1
    spent = realized = 0.0
    n_buy = n_close = 0
    for r in book["log"]:
        if not match(r.get("title")):
            continue
        if r.get("act") == "BUY":
            spent += r.get("spend", 0) or 0
            n_buy += 1
        if "pnl" in r:
            realized += r.get("pnl", 0) or 0
            n_close += 1
    unreal = val_open - inv_open
    total = realized + unreal
    return jsonify({"kw": kws, "n_buy": n_buy, "n_close": n_close, "n_open": n_open,
                    "spent": round(spent, 2), "realized": round(realized, 2),
                    "unrealized": round(unreal, 2), "total": round(total, 2),
                    "pct_on_spent": round(total / spent * 100, 2) if spent else 0.0})


def main():
    p = argparse.ArgumentParser(description="Веб-дашборд бумажного копи")
    p.add_argument("--wallets", help="адреса целей через запятую")
    p.add_argument("--from-watchlist", help="взять цели из ranked_watchlist.json")
    p.add_argument("--bankroll", type=float, default=10_000)
    p.add_argument("--per-trade", type=float, default=10)
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
    STATE["state_file"] = args.state                # путь книги (для эндпоинта пересчёта)
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
