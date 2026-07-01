#!/usr/bin/env python3
"""Один цикл копи для GitHub Actions (serverless, без сервера/ноута).
Загружает книгу -> один проход cycle() -> сохраняет книгу + статический snapshot для дашборда.
Состояние коммитится обратно в репозиторий воркфлоу-ом."""
import json
import os
import time

import copy_trader as ct
import copy_dashboard as cd

BOOK = "paper_book.json"
WL = "copy_watchlist.json"
BANKROLL = float(os.environ.get("BANKROLL", "1000"))
PER_TRADE = float(os.environ.get("PER_TRADE", "1"))

book = ct.load_book(BOOK, BANKROLL)
wallets = [w.lower() for w in json.load(open(WL, encoding="utf-8"))["watchlist"]]
api = ct.API()
marks = ct.cycle(api, book, wallets, PER_TRADE, 0.01)

# точка кривой PnL
ov = 0.0
for p in book["positions"].values():
    mk = marks.get(p["token"])
    ov += p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]
hist = book.setdefault("pnl_history", [])
hist.append([int(time.time()), round(book["realized"], 2), round(book["cash"] + ov - book["bankroll"], 2)])
if len(hist) > 2000:
    del hist[:len(hist) - 2000]
ct.save_book(BOOK, book)

# статический снапшот для GitHub Pages (дашборд читает docs/state.json)
data = cd.compute_stats(book, marks)
data["status"] = {"last_poll": int(time.time()), "wallets": len(wallets),
                  "n_polls": 0, "polling": False, "error": ""}
os.makedirs("docs", exist_ok=True)
json.dump(data, open("docs/state.json", "w", encoding="utf-8"), ensure_ascii=False)

# часовой perf-снимок (временной ряд для прополки)
with open("perf_history.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": int(time.time()), "realized": data["realized"],
                        "unrealized": data["unrealized"], "pnl": data["pnl"],
                        "n_open": data["n_open"]}, ensure_ascii=False) + "\n")

print(f"cycle done | pnl={data['pnl']} open={data['n_open']} realized={data['realized']}")
